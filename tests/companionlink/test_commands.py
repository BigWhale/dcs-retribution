"""The client of DCS Companion's command port, against a fake companion.

The fake answers `POST /v1/spawn` and `GET /v1/spawns/{id}` from a real HTTP
server on 127.0.0.1 as the companion does (`docs/reference/campaign.md`,
Command port): `202 {"id":"s-1"}`, the spawn record under `spawn`, errors as
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

from game.companionlink.commands import CommandError, CompanionCommands, SpawnRequest

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
        self.statuses: list[str] = []
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
