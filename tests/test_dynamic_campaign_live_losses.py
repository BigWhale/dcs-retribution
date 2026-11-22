"""A dynamic campaign's live record: the name map, the mission marker and
which live mission is the campaign's own.

Begin Campaign's mission carries a hidden marker static; a live mission is
the campaign's when its snapshot names that static. The name map is the
mission's unit map, kept without pydcs so the game saves it. Entries of the
event log wait until their mission is decided.

The games are generated headless from a campaign shipped with Retribution,
created as the new game wizard does: the flag first, then `begin_turn_0`.
The campaign has scenery targets, circles and quads both.
"""

from __future__ import annotations

import io
import logging
import math
import pickle
import pickletools
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from dcs import Mission

from game import persistency
from game.ato.flight import Flight
from game.companionlink.eventlog import EventBatch, LogEntry, LogRef
from game.companionlink.link import LinkStatus
from game.companionlink.liverecord import (
    MARKER_PREFIX,
    LiveRecord,
    MissionVerdict,
    companion_status,
)
from game.companionlink.liveunitmap import LiveUnitMap, ZoneShape
from game.companionlink.snapshot import LiveSnapshot, LiveUnit
from game.dcs.groundunittype import GroundUnitType
from game.game import Game
from game.migrator import Migrator
from game.sim.missionsimulation import MissionSimulation
from game.squadrons import Pilot
from game.theater import ControlPoint, TheaterUnit
from game.transfers import CargoShip, Convoy, TransferOrder
from tests.test_dynamic_campaign_air_wing import (
    generate_starting_mission,
    new_game,
    plan_by_hand,
    saved_games,  # noqa: F401 (the fixture)
)

CAMPAIGN = Path("resources/campaigns/golan_heights_lite.yaml")


@dataclass
class Started:
    game: Game
    uuid: str
    sim: MissionSimulation
    miz: Path
    planned: Flight


@pytest.fixture(scope="module")
def started(saved_games: Path, tmp_path_factory: pytest.TempPathFactory) -> Started:
    """Begin Campaign's mission, with one flight planned by hand."""
    game = new_game(dynamic=True, squadrons_start_full=True, campaign_file=CAMPAIGN)
    planned = plan_by_hand(game, 2)
    game.pass_turn(no_action=True)
    uuid = game.live.new_marker()
    folder = tmp_path_factory.mktemp("mission")
    sim = generate_starting_mission(game, folder)
    assert sim.unit_map is not None
    game.live.written(LiveUnitMap.from_generation(sim.unit_map, game))
    return Started(game, uuid, sim, folder / "start.miz", planned)


def written_map(started: Started) -> LiveUnitMap:
    name_map = started.game.live.written_map
    assert name_map is not None
    return name_map


def snapshot(
    boot: str, session: int, *names: str, minute: int = 0, units: int = 0
) -> LiveSnapshot:
    live = tuple(
        LiveUnit(f"u{i}", "g", "ground", "T-72", 1, 0, 0, 0, 0, 0, False, None)
        for i in range(units)
    )
    return LiveSnapshot(
        minute=minute,
        boot=boot,
        session=session,
        model_time=0,
        units=live,
        size=0,
        names=frozenset(names),
    )


def entry(seq: int, boot: str, session: int) -> LogEntry:
    return LogEntry(seq, "dead", boot, session, unit=LogRef(f"unit {seq}"))


def test_starting_mission_has_marker_and_name_map(started: Started) -> None:
    mission = Mission()
    mission.load_file(str(started.miz))
    statics = [
        (group, unit)
        for coalition in mission.coalition.values()
        for country in coalition.countries.values()
        for group in country.static_group
        for unit in group.units
    ]
    markers = [
        (group, unit)
        for group, unit in statics
        if str(unit.name) == f"dcs-companion-mission|{started.uuid}"
    ]
    assert len(markers) == 1
    [(group, _)] = markers
    assert group.hidden
    assert group.hidden_on_planner
    assert len([u for _, u in statics if str(u.name).startswith(MARKER_PREFIX)]) == 1

    name_map = written_map(started)
    assert name_map.theater_objects
    assert name_map.front_line_units
    assert any(
        cast(ZoneShape, m.trigger_zone).corners is None
        for m in name_map.scenery_objects.values()
    )
    assert any(
        cast(ZoneShape, m.trigger_zone).corners is not None
        for m in name_map.scenery_objects.values()
    )
    parked = [u for u in name_map.aircraft.values() if u.flight is not started.planned]
    assert parked
    assert all(u.pilot is None for u in parked)
    # The flight planned by hand keeps its pilots.
    planned = name_map.flight_units(started.planned)
    assert len(planned) == 2
    assert all(name_map.aircraft[n].pilot is not None for n in planned)


#: The game's own objects, which the save already pickles; the name map may
#: point at them.
GAME_OBJECTS = (
    TheaterUnit,
    Flight,
    Pilot,
    ControlPoint,
    GroundUnitType,
    Convoy,
    CargoShip,
    TransferOrder,
)


def pickled_modules(data: bytes) -> set[str]:
    """The module of every class or function the pickle names."""
    modules: set[str] = set()
    strings: list[str] = []
    for op, arg, _ in pickletools.genops(data):
        if isinstance(arg, str):
            strings.append(arg)
        if op.name == "STACK_GLOBAL":
            modules.add(strings[-2])
        elif op.name in ("GLOBAL", "INST"):
            modules.add(str(arg).split(" ")[0])
    return modules


def test_name_map_pickles_without_pydcs(started: Started) -> None:
    name_map = written_map(started)

    data = pickle.dumps(name_map)

    for module in (b"dcs.mission", b"dcs.translation", b"dcs.unit\n"):
        assert module not in data
    assert not {"dcs.mission", "dcs.translation", "dcs.unit"} & pickled_modules(data)

    # The map's own objects hold nothing of pydcs; what it points at in the game
    # (a campaign's scenery unit keeps its pydcs trigger zone) the save already
    # pickles, from the game down (unpickled from the map up, the game would
    # be restored before its theater).
    game_objects: dict[int, Any] = {}

    class MapOnly(pickle.Pickler):
        def persistent_id(self, obj: Any) -> Any:
            if isinstance(obj, GAME_OBJECTS):
                game_objects[id(obj)] = obj
                return id(obj)
            return None

    class GameObjects(pickle.Unpickler):
        def persistent_load(self, pid: Any) -> Any:
            return game_objects[pid]

    out = io.BytesIO()
    MapOnly(out).dump(name_map)
    assert not {m for m in pickled_modules(out.getvalue()) if m.startswith("dcs")}
    out.seek(0)
    loaded = GameObjects(out).load()
    for field, value in vars(name_map).items():
        assert getattr(loaded, field).keys() == value.keys(), field


def test_scenery_found_by_position(started: Started) -> None:
    name_map = written_map(started)
    zones = [cast(ZoneShape, m.trigger_zone) for m in name_map.scenery_objects.values()]
    circles = [z for z in zones if z.corners is None]
    # A circle with no other zone near enough to answer for it.
    zone = next(
        z
        for z in circles
        if all(o is z or math.hypot(o.x - z.x, o.y - z.y) > 1000 for o in zones)
    )

    assert name_map.scenery_at(zone.x + 20, zone.y) == zone.name
    assert name_map.scenery_at(zone.x + 500, zone.y) is None

    quad = next(z for z in zones if z.corners is not None)
    assert name_map.scenery_at(quad.x, quad.y) == quad.name


def test_missions_decided_by_marker(caplog: pytest.LogCaptureFixture) -> None:
    record = LiveRecord()
    uuid = record.new_marker()
    marker = MARKER_PREFIX + uuid

    assert record.decide(snapshot("boot-a", 1, marker, "Uzi 1-1")) is MissionVerdict.OWN
    assert record.decide(snapshot("boot-a", 2, "Uzi 1-1")) is MissionVerdict.FOREIGN
    assert (
        record.decide(snapshot("boot-a", 2, "Uzi 1-1", marker))
        is MissionVerdict.FOREIGN
    ), "a decided mission keeps its verdict"
    assert record.decide(snapshot("boot-b", 1, marker)) is MissionVerdict.OWN_AGAIN
    assert record.decide(snapshot("boot-a", 1, marker)) is MissionVerdict.OWN

    old = LiveRecord()  # a save from before the marker
    with caplog.at_level(logging.INFO):
        for session in (1, 2):
            verdict = old.decide(snapshot("boot-a", session, MARKER_PREFIX + "x"))
            assert verdict is MissionVerdict.FOREIGN
    told = "Live losses: this save has no mission marker; start a new dynamic campaign"
    assert caplog.text.count(told) == 1


def test_entries_wait_for_their_mission() -> None:
    """Review Focus 2: entries of a mission before its first snapshot wait."""
    record = LiveRecord()
    marker = MARKER_PREFIX + record.new_marker()
    record.written(LiveUnitMap())
    record.receive(EventBatch((), through=10))
    assert record.cursor == 10

    record.receive(EventBatch((entry(9, "b", 1), entry(11, "b", 1)), through=11))
    record.receive(EventBatch((entry(12, "b", 1), entry(13, "c", 1)), through=13))
    assert record.ready() == []
    assert record.cursor == 10

    assert record.decide(snapshot("b", 1, marker)) is MissionVerdict.OWN
    assert [e.seq for e in record.ready()] == [11, 12]  # 13's mission undecided
    assert record.cursor == 10
    record.applied(12)
    assert record.cursor == 12

    assert record.decide(snapshot("c", 1)) is MissionVerdict.FOREIGN
    assert record.ready() == []
    assert record.cursor == 13

    # A mission written later (a restore) takes over: the old one's entries
    # still waiting are dropped once the new one is accepted.
    record.receive(EventBatch((entry(14, "b", 1), entry(15, "d", 1)), through=15))
    new_marker = MARKER_PREFIX + record.new_marker()
    record.written(LiveUnitMap())
    assert record.decide(snapshot("d", 1, new_marker)) is MissionVerdict.OWN
    assert [e.seq for e in record.ready()] == [15]
    assert record.cursor == 14


def test_lost_mission_is_not_followed() -> None:
    """Review Focus 5: only the campaign's own mission is followed."""
    record = LiveRecord()
    marker = MARKER_PREFIX + record.new_marker()
    followed = [record.following_own_mission]

    own = snapshot("a", 1, marker, minute=12, units=1234)
    record.decide(own)
    followed.append(record.following_own_mission)
    record.lose()
    followed.append(record.following_own_mission)
    assert record.decide(snapshot("a", 1, marker)) is MissionVerdict.OWN
    followed.append(record.following_own_mission)
    record.decide(snapshot("a", 2))
    followed.append(record.following_own_mission)
    record.decide(snapshot("b", 1, marker))
    followed.append(record.following_own_mission)
    assert followed == [False, True, False, True, False, True]

    foreign = snapshot("a", 2, minute=3, units=5)
    status = companion_status
    assert status(LinkStatus.WAITING, None, False, None) == "Companion: not running"
    assert (
        status(LinkStatus.REFUSED, own, False, MissionVerdict.OWN)
        == "Companion: refused (token)"
    )
    waiting = "Companion: waiting for a mission on the server"
    assert status(LinkStatus.LINKED, None, False, None) == waiting
    assert status(LinkStatus.RECEIVING, own, True, MissionVerdict.OWN) == waiting
    assert (
        status(LinkStatus.RECEIVING, foreign, False, MissionVerdict.FOREIGN)
        == "Companion: another mission is running; waiting for this campaign's"
    )
    assert (
        status(LinkStatus.RECEIVING, own, False, MissionVerdict.OWN)
        == "Companion: minute 12, 1,234 units"
    )
    assert (
        status(LinkStatus.RECEIVING, own, False, MissionVerdict.OWN_AGAIN)
        == "Companion: minute 12, 1,234 units (mission restarted: earlier losses stay)"
    )


def test_record_survives_save_and_load(started: Started, tmp_path: Path) -> None:
    game = started.game
    record = game.live
    marker = MARKER_PREFIX + started.uuid
    record.decide(snapshot("boot", 7, marker))
    record.applied(42)
    record.dead.add("Uzi 1-1")
    record.receive(EventBatch((entry(43, "boot", 7),), through=43))
    game.savepath = str(tmp_path / "live.retribution")

    assert persistency.save_game(game)
    loaded = persistency.load_game(game.savepath)
    assert loaded is not None
    Migrator(loaded, is_liberation=False)

    live = loaded.live
    assert live.marker == started.uuid
    assert live.verdicts == record.verdicts
    assert live.cursor == 42
    assert live.dead == {"Uzi 1-1"}
    assert live.map_marker == started.uuid
    for field, value in vars(record.name_map).items():
        assert getattr(live.name_map, field).keys() == value.keys(), field
    assert live.ready() == []  # held entries are not saved; the link reads again
