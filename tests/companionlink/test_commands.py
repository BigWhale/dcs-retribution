"""The client of DCS Companion's command port, against a fake companion.

The fake (`FakeCommands`, in conftest) answers `POST /v1/spawn`,
`GET /v1/spawns`, `GET /v1/spawns/{id}`, `POST /v1/despawn` and
`POST /v1/mission/load` from a real HTTP server on 127.0.0.1 as the companion
does (`docs/reference/campaign.md`, Command port): `202 {"id":"s-1"}`, the
mission's spawns under `spawns`, the spawn record under `spawn`, a despawn's
`group` and `units`, a load's `202 {"file","boot","session"}`, errors as
`{"error": "..."}` and a wrong token `401`.
"""

from __future__ import annotations

import logging
import socket
import time
from typing import Any, Iterator

import pytest

from game.companionlink.commands import (
    CommandError,
    CompanionCommands,
    SpawnFailed,
    SpawnPending,
    SpawnRequest,
    SpawnsView,
    despawn_and_spawn,
)
from game.companionlink.linkstatus import LinkStatus
from tests.conftest import COMMAND_TOKEN as TOKEN
from tests.conftest import FakeCommands

GROUP: dict[str, Any] = {
    "name": "LINK 1",
    "groupId": 7001,
    "units": {"1": {"name": "LINK 1-1", "type": "F-16C_50", "skill": "High"}},
    "route": {"points": {}},
}


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


def test_held_names_round_trip(fake: FakeCommands) -> None:
    """The names the companion holds: its spawns waiting, unknown or spawned."""
    fake.spawns = [
        (1, "Uzi 1", "spawned"),
        (2, "Uzi 2", "waiting"),
        (3, "Uzi 3", "unknown"),
        (4, "Uzi 4", "failed"),
        (5, "Uzi 5", "despawned"),
    ]

    held = CompanionCommands(fake.address, TOKEN).held_names()

    assert held == frozenset({"Uzi 1", "Uzi 2", "Uzi 3"})
    assert len(fake.lists) == 1
    assert fake.lists[0]["Authorization"] == f"Bearer {TOKEN}"


def test_spawn_refused_after_despawn_says_so(fake: FakeCommands) -> None:
    """The spares are gone when the spawn is refused: the text says so."""
    fake.groups = {"Spare 1": 1, "Spare 2": 1}
    fake.refuse = (409, "the name is held by s-10 (spawned)")

    with pytest.raises(CommandError) as refused:
        despawn_and_spawn(
            CompanionCommands(fake.address, TOKEN), ["Spare 1", "Spare 2"], request()
        )

    assert refused.value.status == 409
    assert str(refused.value) == (
        "spawn of 'LINK 1' failed: the companion answered 409: "
        "the name is held by s-10 (spawned); "
        "the parked spares 'Spare 1', 'Spare 2' were despawned for it"
    )
    assert fake.groups == {}


def test_spawns_view(fake: FakeCommands) -> None:
    """The mission's spawns as the companion sees them: ids and statuses."""
    fake.spawns = [(3, "Uzi 1", "waiting"), (12, "Uzi 2", "failed")]
    fake.spawns_mission = ("b", 2, 61.5)

    view = CompanionCommands(fake.address, TOKEN).spawns()

    assert view == SpawnsView("b", 2, 61.5, ((3, "waiting"), (12, "failed")))
    assert fake.lists[0]["Authorization"] == f"Bearer {TOKEN}"


def test_load_mission_round_trip(fake: FakeCommands) -> None:
    CompanionCommands(fake.address, TOKEN).load_mission("camp.miz")

    assert fake.loads == ["camp.miz"]


def test_load_mission_refused(
    fake: FakeCommands, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="game.companionlink")
    fake.load_refuse = (404, "load_mission: no such file camp.miz")

    with pytest.raises(CommandError) as refused:
        CompanionCommands(fake.address, TOKEN).load_mission("camp.miz")

    assert refused.value.status == 404
    assert refused.value.message == "load_mission: no such file camp.miz"
    assert str(refused.value) == (
        "the companion answered 404: load_mission: no such file camp.miz"
    )
    assert TOKEN not in str(refused.value)
    assert TOKEN not in caplog.text


def test_reachable(fake: FakeCommands) -> None:
    """The command port answers (linked), refuses the token, or is not there."""
    assert CompanionCommands(fake.address, TOKEN).reachable() is LinkStatus.LINKED
    assert CompanionCommands(fake.address, "bad").reachable() is LinkStatus.REFUSED

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    closed = CompanionCommands(f"http://127.0.0.1:{port}", TOKEN, timeout=1.0)
    assert closed.reachable() is LinkStatus.WAITING
