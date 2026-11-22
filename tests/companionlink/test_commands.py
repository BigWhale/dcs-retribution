"""The client of DCS Companion's command port, against a fake companion.

The fake answers `POST /v1/spawn`, `GET /v1/spawns/{id}` and `POST /v1/despawn`
from a real HTTP server on 127.0.0.1 as the companion does
(`docs/reference/campaign.md`, Command port): `202 {"id":"s-1"}`, the spawn
record under `spawn`, a despawn's `group` and `units`, errors as
`{"error": "..."}` and a wrong token `401`.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator, Optional

import pytest

from game.companionlink.commands import (
    CommandError,
    CompanionCommands,
    SpawnFailed,
    SpawnPending,
    SpawnRequest,
    despawn_and_spawn,
)

TOKEN = "tok-c"

GROUP: dict[str, Any] = {
    "name": "LINK 1",
    "groupId": 7001,
    "units": {"1": {"name": "LINK 1-1", "type": "F-16C_50", "skill": "High"}},
    "route": {"points": {}},
}


class FakeCommands:
    """The companion's command port: spawn requests and their records."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[tuple[dict[str, Any], dict[str, str]]] = []
        #: The answer to the next POST /v1/spawn, or None for 202 s-1.
        self.refuse: Optional[tuple[int, str]] = None
        #: The statuses GET /v1/spawns/s-1 gives, in turn; the last repeats.
        #: "http:503" answers that HTTP status with an error instead.
        self.statuses: list[str] = []
        #: The groups POST /v1/despawn removes, with their units; others 404.
        self.groups: dict[str, int] = {}
        #: The status every POST /v1/despawn answers with an error, or None.
        self.despawn_refuse: Optional[int] = None
        #: The bodies and headers of POST /v1/despawn, in order.
        self.despawns: list[tuple[dict[str, Any], dict[str, str]]] = []
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def authorized(self) -> bool:
                if self.headers.get("Authorization") == f"Bearer {TOKEN}":
                    return True
                self.answer(401, {"error": "unauthorized"})
                return False

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if not self.authorized():
                    return
                if self.path == "/v1/despawn":
                    self.despawn(json.loads(raw))
                    return
                if self.path != "/v1/spawn":
                    self.answer(404, {"error": "not found"})
                    return
                with fake.lock:
                    fake.requests.append((json.loads(raw), dict(self.headers)))
                    refuse = fake.refuse
                if refuse is not None:
                    self.answer(refuse[0], {"error": refuse[1]})
                    return
                self.answer(202, {"id": "s-1"})

            def do_GET(self) -> None:
                if not self.authorized():
                    return
                if self.path != "/v1/spawns/s-1":
                    self.answer(404, {"error": "no spawn s-2"})
                    return
                with fake.lock:
                    status = (
                        fake.statuses.pop(0)
                        if len(fake.statuses) > 1
                        else fake.statuses[0]
                    )
                if status.startswith("http:"):
                    self.answer(int(status[5:]), {"error": "not now"})
                    return
                spawn: dict[str, Any] = {
                    "id": "s-1",
                    "name": "LINK 1",
                    "country": 2,
                    "category": "plane",
                    "mission": {"boot": "b1", "session": 1},
                    "status": status,
                    "asked": "2026-11-22T10:00:00.000Z",
                }
                if status == "failed":
                    spawn["reason"] = "campaign_spawn: group name in use"
                self.answer(
                    200,
                    {"boot": "b1", "session": 1, "modelTime": 2401.2, "spawn": spawn},
                )

            def despawn(self, body: dict[str, Any]) -> None:
                with fake.lock:
                    fake.despawns.append((body, dict(self.headers)))
                    refuse = fake.despawn_refuse
                    name = str(body.get("name"))
                    units = None if refuse else fake.groups.pop(name, None)
                if refuse is not None:
                    self.answer(refuse, {"error": "hook link down"})
                    return
                if units is None:
                    self.answer(404, {"error": f"campaign_despawn: no group {name}"})
                    return
                self.answer(
                    200,
                    {
                        "boot": "b1",
                        "session": 1,
                        "modelTime": 2410.5,
                        "group": name,
                        "units": units,
                    },
                )

            def answer(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                if status == 401:
                    self.send_header("WWW-Authenticate", "Bearer")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server = server
        self.thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    @property
    def address(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join()


@pytest.fixture
def fake() -> Iterator[FakeCommands]:
    f = FakeCommands()
    f.start()
    yield f
    f.close()


def request() -> SpawnRequest:
    return SpawnRequest(name="LINK 1", country=2, category="plane", group=GROUP)


def test_spawn_sends_the_request(fake: FakeCommands) -> None:
    spawn_id = CompanionCommands(fake.address, TOKEN).spawn(request())

    assert spawn_id == "s-1"
    assert len(fake.requests) == 1
    body, headers = fake.requests[0]
    assert body == {"name": "LINK 1", "country": 2, "category": "plane", "group": GROUP}
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Content-Type"] == "application/json"


def test_spawn_refused_raises(
    fake: FakeCommands, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="game.companionlink")
    fake.refuse = (409, "name taken")
    with pytest.raises(CommandError) as taken:
        CompanionCommands(fake.address, TOKEN).spawn(request())
    assert taken.value.status == 409
    assert "name taken" in taken.value.message

    fake.refuse = None
    with pytest.raises(CommandError) as refused:
        CompanionCommands(fake.address, "bad").spawn(request())
    assert refused.value.status == 401
    assert "bad" not in str(refused.value)
    assert "bad" not in refused.value.message
    assert TOKEN not in str(taken.value)
    assert TOKEN not in caplog.text
    assert "Bearer bad" not in caplog.text


def test_wait_spawned(fake: FakeCommands) -> None:
    commands = CompanionCommands(fake.address, TOKEN)

    fake.statuses = ["waiting", "unknown", "spawned"]
    commands.wait_spawned("s-1", limit=3.0, poll=0.01)

    fake.statuses = ["waiting", "failed"]
    with pytest.raises(CommandError) as failed:
        commands.wait_spawned("s-1", limit=3.0, poll=0.01)
    assert "group name in use" in failed.value.message

    fake.statuses = ["waiting"]
    started = time.monotonic()
    with pytest.raises(CommandError) as late:
        commands.wait_spawned("s-1", limit=0.2, poll=0.01)
    assert late.value.status is None
    assert time.monotonic() - started < 2.0

    with pytest.raises(CommandError) as missing:
        commands.spawn_status("s-2")
    assert missing.value.status == 404


def test_wait_spawned_tells_pending_from_failed(fake: FakeCommands) -> None:
    """A spawn still waiting at the limit may yet spawn; a failed one never."""
    commands = CompanionCommands(fake.address, TOKEN)

    fake.statuses = ["waiting"]
    with pytest.raises(SpawnPending) as pending:
        commands.wait_spawned("s-1", limit=0.1, poll=0.01)
    assert pending.value.last_status == "waiting"
    assert "not spawned within" in str(pending.value)

    fake.statuses = ["despawned"]
    with pytest.raises(SpawnFailed) as gone:
        commands.wait_spawned("s-1", limit=3.0, poll=0.01)
    assert not isinstance(gone.value, SpawnPending)
    assert "despawned" in gone.value.message

    fake.statuses = ["waiting", "spawned"]
    with pytest.raises(SpawnPending):
        commands.wait_spawned("s-1", limit=0.0, poll=0.01)
    commands.wait_spawned("s-1", limit=3.0, poll=0.01)


def test_connect_error() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    commands = CompanionCommands(f"http://127.0.0.1:{port}", TOKEN, timeout=1.0)
    started = time.monotonic()
    with pytest.raises(CommandError) as error:
        commands.spawn(request())
    assert error.value.status is None
    assert time.monotonic() - started < 2.0
    assert TOKEN not in str(error.value)


def test_despawn_round_trip(fake: FakeCommands) -> None:
    fake.groups = {"Spare 1": 1}

    units = CompanionCommands(fake.address, TOKEN).despawn("Spare 1")

    assert units == 1
    assert len(fake.despawns) == 1
    body, headers = fake.despawns[0]
    assert body == {"name": "Spare 1"}
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Content-Type"] == "application/json"


def test_despawn_gone_group_then_spawn(fake: FakeCommands) -> None:
    """A spare already gone (404) is fine: the flight is spawned on its spot."""
    commands = CompanionCommands(fake.address, TOKEN)
    assert commands.despawn("Spare 1") is None

    fake.groups = {"Spare 2": 2}
    spawn_id = despawn_and_spawn(commands, ["Spare 1", "Spare 2"], request())

    assert spawn_id == "s-1"
    assert [body["name"] for body, _ in fake.despawns] == [
        "Spare 1",
        "Spare 1",
        "Spare 2",
    ]
    assert len(fake.requests) == 1


def test_despawn_failure_stops_before_spawn(
    fake: FakeCommands, caplog: pytest.LogCaptureFixture
) -> None:
    """A despawn that may not have happened: nothing spawns over the spare."""
    caplog.set_level(logging.DEBUG, logger="game.companionlink")
    commands = CompanionCommands(fake.address, TOKEN)
    fake.groups = {"Spare 1": 1, "Spare 2": 1}

    for status in (503, 504, 409):
        fake.despawn_refuse = status
        with pytest.raises(CommandError) as failed:
            despawn_and_spawn(commands, ["Spare 1", "Spare 2"], request())
        assert failed.value.status == status
        assert str(failed.value).startswith(
            f"despawn of 'Spare 1' failed: the companion answered {status}: "
        )
        assert TOKEN not in str(failed.value)

    assert [body["name"] for body, _ in fake.despawns] == ["Spare 1"] * 3
    assert fake.requests == []
    assert TOKEN not in caplog.text
