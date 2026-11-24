"""A dynamic campaign's restored mission: the newest snapshot of the campaign's
own mission (the tick) kept in the save, and the mission written again from it.

The link hands each snapshot over with its envelope compressed; the live
record keeps the newest one whose mission is the campaign's own under the
name map's marker, so the tick and the name map always belong to the same
mission. The tick is saved with the game; a save from before it loads with
none.

A restore writes the campaign's mission again at the tick's minute, read back
here with pydcs: no flights, the aircraft not in the air parked, a new marker;
the flights are sorted from the tick and the replaced mission is retired.

The games are generated headless from a campaign shipped with Retribution,
created as the new game wizard does: the flag first, then `begin_turn_0`.
"""

from __future__ import annotations

import json
import logging
import math
import pickle
import zlib
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, cast

import pytest
from dcs.mission import Mission
from dcs.unitgroup import FlyingGroup

from game import persistency
from game.ato.flight import Flight
from game.companionlink.commands import SpawnsView
from game.companionlink.eventlog import EventBatch, LogEntry, LogPos, LogRef
from game.companionlink.livelosses import apply_entries
from game.companionlink.liverecord import MARKER_PREFIX, MissionVerdict
from game.companionlink.liveunitmap import LiveUnitMap
from game.companionlink.restore import (
    RestoreError,
    RestorePlacement,
    restore_start,
    sort_launched,
    write_restore,
)
from game.companionlink.snapshot import LiveSnapshot, parse_snapshot
from game.data.units import UnitClass
from game.game import Game
from game.missiongenerator import MissionGenerator
from game.sim.missionsimulation import MissionSimulation
from game.squadrons import Squadron
from game.theater import Airfield, ControlPoint, Fob
from game.theater.theatergroundobject import ShipGroundObject
from game.transfers import TransferOrder
from tests.conftest import (
    generate_starting_mission,
    new_game,
    plan_by_hand,
    save_and_load,
    squadrons,
)

CAMPAIGN = Path("resources/campaigns/golan_heights_lite.yaml")
#: When the snapshots' missions started.
START = datetime(2015, 6, 1, 8, 0)
#: A coalition's `side` in a snapshot.
SIDES = {"blue": 2, "red": 1, "neutrals": 0}
#: A pydcs group list's `cat` in a snapshot.
CATEGORIES = {
    "plane_group": 0,
    "helicopter_group": 1,
    "vehicle_group": 2,
    "ship_group": 3,
}


@pytest.fixture(scope="module")
def start_miz(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Where Begin Campaign's mission is written."""
    return tmp_path_factory.mktemp("mission") / "start.miz"


@pytest.fixture(scope="module")
def begun(saved_games: Path, start_miz: Path) -> Game:
    """Begin Campaign's game: its mission written with a marker and name map."""
    game = new_game(dynamic=True, squadrons_start_full=True, campaign_file=CAMPAIGN)
    game.pass_turn(no_action=True)
    game.live.new_marker()
    sim = generate_starting_mission(game, start_miz.parent)
    assert sim.unit_map is not None
    game.live.written(LiveUnitMap.from_generation(sim.unit_map, game))
    return game


@pytest.fixture
def game(begun: Game) -> Game:
    """A copy of Begin Campaign's game, for a test that changes it."""
    return copy(begun)


@pytest.fixture(scope="module")
def running(begun: Game, start_miz: Path) -> Game:
    """Begin Campaign's game with its mission running: decided the campaign's
    own by a snapshot of its marker, kept as the tick."""
    game = copy(begun)
    tick_of(game, start_miz)
    return game


@pytest.fixture
def own(running: Game) -> Game:
    """A copy of the running campaign, for a test that changes it."""
    return copy(running)


def copy(game: Game) -> Game:
    """A copy of the game; a copy is not a loaded save: it awaits no pick-up."""
    copied = cast(Game, pickle.loads(pickle.dumps(game)))
    copied.live.end_pickup()
    return copied


def envelope(
    game: Game,
    boot: str,
    minute: int,
    own: bool = True,
    timed: bool = True,
    groups: Optional[list[dict[str, Any]]] = None,
) -> str:
    """The companion's snapshot of the mission `boot` (session 1) at its
    `minute`, the mission started at START; the campaign's own carries the
    marker written last, an untimed one has no date or absTime."""
    statics = [{"name": MARKER_PREFIX + str(game.live.marker), "id": 9000}]
    at = START + timedelta(minutes=minute)
    midnight = datetime(at.year, at.month, at.day)
    meta: dict[str, Any] = {"modelTime": 60.0 * minute}
    if timed:
        meta["date"] = {"Year": at.year, "Month": at.month, "Day": at.day}
        meta["absTime"] = (at - midnight).total_seconds()
    return json.dumps(
        {
            "minute": minute,
            "boot": boot,
            "session": 1,
            "modelTime": 60.0 * minute,
            "state": {
                "meta": meta,
                "groups": groups or [],
                "statics": statics if own else [],
                "airbases": [],
            },
        }
    )


def tank(name: str, x: float, y: float) -> dict[str, Any]:
    """A ground group of one tank, `name` and its unit `name-1`."""
    return {
        "name": name,
        "cat": 2,
        "id": 100,
        "side": 1,
        "units": [
            {"name": f"{name}-1", "id": 101, "type": "T-55", "x": x, "y": y, "hdg": 90}
        ],
    }


def taken(data: str) -> LiveSnapshot:
    """The snapshot as the link hands it over: parsed, its envelope packed."""
    return replace(parse_snapshot(data), packed=zlib.compress(data.encode(), 6))


def test_tick_kept_only_for_the_name_maps_mission(game: Game) -> None:
    record = game.live
    own = taken(envelope(game, "b1", 3, groups=[tank("Armor", 1000.0, -2000.0)]))
    assert record.decide(own) is MissionVerdict.OWN
    assert record.keep(own)
    tick = record.tick
    assert tick is not None
    assert (tick.boot, tick.session, tick.minute) == ("b1", 1, 3)
    assert tick.model_time == 180.0
    assert tick.abs_time == START + timedelta(minutes=3)
    restored = tick.snapshot()
    assert restored.names == own.names
    assert restored.units == own.units

    foreign = taken(envelope(game, "b2", 4, own=False))
    assert record.decide(foreign) is MissionVerdict.FOREIGN
    assert not record.keep(foreign)
    assert record.tick is tick

    untimed = taken(envelope(game, "b1", 5, timed=False))
    assert record.decide(untimed) is MissionVerdict.OWN
    assert not record.keep(untimed), "a restore needs the minute"
    assert record.tick is tick

    unpacked = parse_snapshot(envelope(game, "b1", 5))
    assert not record.keep(unpacked)
    assert record.tick is tick

    # The campaign's mission loaded again from its start is not followed.
    again = taken(envelope(game, "b3", 6))
    assert record.decide(again) is MissionVerdict.OWN_AGAIN
    assert not record.keep(again)
    assert record.tick is tick

    # A newer mission written and loaded: the name map is its, and a snapshot
    # of the earlier mission, still running, is not kept.
    record.new_marker()
    record.written(LiveUnitMap())
    newer = taken(envelope(game, "b4", 1))
    assert record.decide(newer) is MissionVerdict.OWN
    assert record.keep(newer)
    late = taken(envelope(game, "b3", 7, own=False))  # its marker is the earlier
    assert record.decide(late) is MissionVerdict.OWN_AGAIN
    assert not record.keep(late)
    assert record.tick is not None and record.tick.boot == "b4"


def test_mission_loaded_again_never_replaces_the_tick(game: Game) -> None:
    """DCS stops and loads the campaign's mission file again (lake,
    2026-11-23): the reloaded mission starts over at its first minute. It is
    not followed, so its snapshots never replace the tick, not even once its
    clock passes the tick's: Continue restores the world the tick saw."""
    record = game.live
    ran = taken(envelope(game, "b1", 95))
    assert record.decide(ran) is MissionVerdict.OWN
    assert record.keep(ran)

    reloaded = taken(envelope(game, "b2", 0))
    assert record.decide(reloaded) is MissionVerdict.OWN_AGAIN
    assert not record.keep(reloaded)
    assert record.tick is not None
    assert (record.tick.boot, record.tick.minute) == ("b1", 95)
    assert restore_start(record.tick) == START + timedelta(minutes=95)

    caught_up = taken(envelope(game, "b2", 96))
    assert record.decide(caught_up) is MissionVerdict.OWN_AGAIN
    assert not record.keep(caught_up)
    assert (record.tick.boot, record.tick.minute) == ("b1", 95)


def test_tick_survives_save_and_load(game: Game, tmp_path: Path) -> None:
    own = taken(envelope(game, "b1", 12, groups=[tank("Armor", 1000.0, -2000.0)]))
    game.live.decide(own)
    assert game.live.keep(own)
    tick = game.live.tick
    assert tick is not None

    loaded = save_and_load(game, tmp_path / "tick.retribution").live.tick
    assert loaded is not None
    assert loaded.packed == tick.packed
    assert loaded.minute == 12
    assert loaded.abs_time == START + timedelta(minutes=12)
    assert loaded.snapshot().names == own.names


def test_slice_one_save_loads_without_tick(game: Game, tmp_path: Path) -> None:
    del game.live.__dict__["tick"]
    loaded = save_and_load(game, tmp_path / "slice1.retribution")

    assert loaded.live.tick is None
    record = loaded.live
    own = taken(envelope(loaded, "b1", 2))
    # The mission written and never followed, two minutes in: picked up.
    picked = record.pick_up(own, SpawnsView("b1", 1, own.model_time, ()))
    assert picked.picked
    assert record.decide(own) is MissionVerdict.OWN
    assert record.keep(own)
    assert record.tick is not None and record.tick.minute == 2


def test_dynamic_autosave_is_atomic(
    game: Game, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = persistency.dynamic_autosave_path()
    assert path == persistency.save_dir() / "autosave_dynamic.retribution"
    temporary = path.with_name("autosave_dynamic.retribution.tmp")
    first = taken(envelope(game, "b1", 7))
    game.live.decide(first)
    assert game.live.keep(first)
    assert persistency.autosave(game)

    later = taken(envelope(game, "b1", 8))
    game.live.decide(later)
    assert game.live.keep(later)
    landmap = game.theater.landmap

    def disk_full(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(pickle, "dump", disk_full)
    assert not persistency.autosave(game)
    monkeypatch.undo()

    assert any(r.exc_info is not None for r in caplog.records)
    assert not temporary.exists()
    assert game.theater.landmap is landmap
    kept = persistency.load_game(str(path))
    assert kept is not None and kept.live.tick is not None
    assert kept.live.tick.minute == 7, "the previous autosave is whole"

    # A normal campaign autosaves as before; the dynamic file is left alone.
    normal = persistency.save_dir() / "autosave.retribution"
    normal.unlink(missing_ok=True)
    game.dynamic_campaign = False
    assert persistency.autosave(game)
    saved = persistency.load_game(str(normal))
    assert saved is not None and not saved.dynamic_campaign
    unchanged = persistency.load_game(str(path))
    assert unchanged is not None and unchanged.live.tick is not None
    assert unchanged.live.tick.minute == 7


# The restore.


def read_miz(path: Path) -> Mission:
    mission = Mission()
    mission.load_file(str(path))
    return mission


@lru_cache(maxsize=None)
def miz_objects(path: Path) -> tuple[tuple[dict[str, Any], ...], ...]:
    """The groups and the statics of a written mission, as a snapshot's
    `state.groups` and `state.statics` give them; read once per file."""
    groups: list[dict[str, Any]] = []
    statics: list[dict[str, Any]] = []
    for coalition_name, coalition in read_miz(path).coalition.items():
        side = SIDES[coalition_name]
        for country in coalition.countries.values():
            for attr, cat in CATEGORIES.items():
                for group in getattr(country, attr):
                    units = [
                        {
                            "name": str(u.name),
                            "id": u.id,
                            "type": u.type,
                            "x": u.position.x,
                            "y": u.position.y,
                            "hdg": u.heading,
                            "inAir": False,
                        }
                        for u in group.units
                    ]
                    groups.append(
                        {
                            "name": str(group.name),
                            "cat": cat,
                            "id": group.id,
                            "side": side,
                            "units": units,
                        }
                    )
            for group in country.static_group:
                unit = group.units[0]
                statics.append(
                    {
                        "name": str(unit.name),
                        "id": unit.id,
                        "type": unit.type,
                        "side": side,
                        "x": unit.position.x,
                        "y": unit.position.y,
                        "hdg": unit.heading,
                        "exists": not group.dead,
                    }
                )
    return tuple(groups), tuple(statics)


def snapshot_of(
    game: Game,
    miz: Path,
    *,
    boot: str = "b1",
    moved: Mapping[str, tuple[float, float, float]] = {},
    in_air: frozenset[str] | set[str] = frozenset(),
    gone: frozenset[str] | set[str] = frozenset(),
    extra: Sequence[dict[str, Any]] = (),
    launched: Sequence[Flight] = (),
    at: Optional[datetime] = None,
) -> LiveSnapshot:
    """The companion's snapshot of the mission written to `miz`, running as
    `boot` (session 1) at `at` (by default 37 min 42 s after its start).

    Each group's and static's unit stands where the mission put it, or at
    `moved[name]` (x, y, heading); a unit named in `in_air` flies, one named
    in `gone` is missing. The `extra` groups are added as given. Launched
    flights are added as plane groups under their live names, at their
    departure, without the names counted dead.
    """
    start = game.conditions.start_time
    if at is None:
        at = start + timedelta(minutes=37, seconds=42)
    groups, statics = miz_objects(miz)

    def placed(unit: dict[str, Any]) -> dict[str, Any]:
        unit = dict(unit, inAir=unit["name"] in in_air)
        if unit["name"] in moved:
            unit["x"], unit["y"], unit["hdg"] = moved[unit["name"]]
        return unit

    state_groups = [
        dict(g, units=[placed(u) for u in g["units"] if u["name"] not in gone])
        for g in groups
    ]
    state_groups.extend(extra)
    next_id = 1_000_000
    for flight in launched:
        position = flight.departure.position
        units = []
        for name in game.live.name_map.flight_units(flight):
            if name in game.live.dead:
                continue
            next_id += 1
            units.append(
                placed(
                    {
                        "name": name,
                        "id": next_id,
                        "type": flight.unit_type.dcs_unit_type.id,
                        "x": position.x,
                        "y": position.y,
                        "hdg": 0.0,
                    }
                )
            )
        next_id += 1
        state_groups.append(
            {
                "name": flight.live_group_name,
                "cat": 1 if flight.is_helo else 0,
                "id": next_id,
                "side": 2 if flight.squadron.coalition.player.is_blue else 1,
                "units": units,
            }
        )
    midnight = datetime(at.year, at.month, at.day)
    data = json.dumps(
        {
            "minute": int((at - start).total_seconds() // 60),
            "boot": boot,
            "session": 1,
            "modelTime": (at - start).total_seconds(),
            "state": {
                "meta": {
                    "modelTime": (at - start).total_seconds(),
                    "date": {"Year": at.year, "Month": at.month, "Day": at.day},
                    "absTime": (at - midnight).total_seconds(),
                },
                "groups": state_groups,
                "statics": [placed(s) for s in statics if s["name"] not in gone],
                "airbases": [],
            },
        }
    )
    return taken(data)


def tick_of(game: Game, miz: Path, **kwargs: Any) -> LiveSnapshot:
    """`snapshot_of` decided and kept as the record's tick, in place of any
    tick the game holds (a copied game holds its source's, at the same time)."""
    snapshot = snapshot_of(game, miz, **kwargs)
    game.live.decide(snapshot)
    game.live.tick = None
    assert game.live.keep(snapshot)
    return snapshot


def aircraft_groups(mission: Mission) -> list[FlyingGroup[Any]]:
    return [
        group
        for coalition in mission.coalition.values()
        for country in coalition.countries.values()
        for group in [*country.plane_group, *country.helicopter_group]
    ]


def markers(mission: Mission) -> list[str]:
    return [
        str(group.units[0].name)
        for coalition in mission.coalition.values()
        for country in coalition.countries.values()
        for group in country.static_group
        if str(group.units[0].name).startswith(MARKER_PREFIX)
    ]


def ato_flights(game: Game) -> list[Flight]:
    return [f for c in (game.blue, game.red) for p in c.ato.packages for f in p.flights]


def parked_per_squadron(game: Game, name_map: LiveUnitMap) -> Counter[Squadron]:
    """The aircraft the map holds as parked (not of an ATO flight)."""
    planned = {id(f) for f in ato_flights(game)}
    return Counter(
        u.flight.squadron
        for u in name_map.aircraft.values()
        if id(u.flight) not in planned
    )


def red_squadron(game: Game) -> Squadron:
    """A red squadron at an airfield, with room for four flights of two."""
    return next(
        s
        for s in game.red.air_wing.iter_squadrons()
        if isinstance(s.location, Airfield) and s.untasked_aircraft >= 8
    )


def launch(game: Game, flight: Flight, name: str) -> None:
    """The flight launched into the running mission as the group `name`."""
    flight.launched = True
    flight.live_group_name = name
    game.live.name_map.add_launched(flight, [f"{name}-1", f"{name}-2"])


def test_restore_starts_at_the_minute(
    own: Game, start_miz: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    at = own.conditions.start_time.replace(minute=37, second=42, microsecond=500000)
    tick_of(own, start_miz, at=at)
    old_marker = own.live.marker
    old_map = own.live.written_map
    path = tmp_path / "retribution_dynamic.miz"

    result = write_restore(own, path)

    start = datetime(at.year, at.month, at.day, at.hour, 37)
    assert (result.path, result.start, result.minute) == (path, start, 37)
    assert own.conditions.start_time == start
    mission = read_miz(path)
    assert mission.start_time == start
    assert own.live.marker != old_marker
    assert markers(mission) == [MARKER_PREFIX + str(own.live.marker)]
    written = own.live.written_map
    assert written is not None and written is not old_map
    assert set(written.aircraft) == {
        str(u.name) for g in aircraft_groups(mission) if g.uncontrolled for u in g.units
    }
    assert result.placed > 0 and result.unplaced == 0, "the tick has every unit"
    assert (
        f"Restore: {path} written, starting {start:%Y-%m-%d %H:%M}, from minute 37; "
        f"{result.placed} units placed, 0 where generated; "
        "flights: 0 in the air, 0 to launch again, 0 finished"
    ) in [r.getMessage() for r in caplog.records]


def test_restore_writes_no_flights_and_parks_the_rest(
    own: Game, start_miz: Path, tmp_path: Path
) -> None:
    squadron = red_squadron(own)
    planned, airborne, ramp, landed = (plan_by_hand(own, 2, squadron) for _ in range(4))
    launch(own, airborne, "LIVE 1")
    launch(own, ramp, "LIVE 2")
    launch(own, landed, "LIVE 3")
    own.live.dead.add("LIVE 1-2")
    own.live.landed.add("LIVE 3-1")
    tick = tick_of(
        own, start_miz, in_air={"LIVE 1-1"}, launched=[airborne, ramp, landed]
    )
    assert own.live.written_map is not None
    before = parked_per_squadron(own, own.live.written_map)
    base = [s for s in squadrons(own) if s.location is squadron.location]
    assert all(before[s] == s.owned_aircraft for s in base), "the base parks all"
    placement = RestorePlacement(own, tick, sort_launched(own, tick))
    assert placement.parked(squadron) == squadron.owned_aircraft - 1
    assert all(
        placement.parked(s) == s.owned_aircraft
        for s in squadrons(own)
        if s is not squadron
    )
    landed_pilots = [p for p in landed.roster.iter_pilots() if p is not None]
    path = tmp_path / "retribution_dynamic.miz"

    result = write_restore(own, path)

    assert (result.in_air, result.again, result.finished) == (1, 1, 1)
    mission = read_miz(path)
    written = own.live.written_map
    assert written is not None
    planned_ids = {id(f) for f in ato_flights(own)}
    unmapped = set()
    for group in aircraft_groups(mission):
        names = [str(u.name) for u in group.units]
        if not any(n in written.aircraft for n in names):
            unmapped.add(str(group.name))
            continue
        assert group.uncontrolled, group.name
        assert all(id(written.aircraft[n].flight) not in planned_ids for n in names)
    started = {
        str(g.name)
        for g in aircraft_groups(read_miz(start_miz))
        if not any(str(u.name) in own.live.name_map.aircraft for u in g.units)
    }
    assert unmapped == started, "no flight groups, only the front lines' JTACs"
    after = parked_per_squadron(own, written)
    for s in squadrons(own):
        if s.location is squadron.location:
            assert after[s] == placement.parked(s), s
        else:
            assert after[s] == before[s], s

    assert not ramp.launched and ramp.live_group_name is None
    assert not any(f is landed for f in landed.package.flights)
    assert landed.package not in own.red.ato.packages
    assert airborne.launched and airborne.live_group_name == "LIVE 1"
    assert any(f is planned for f in ato_flights(own)) and not planned.launched
    for s in squadrons(own):
        held = sum(f.count for f in ato_flights(own) if f.squadron is s)
        assert s.untasked_aircraft == s.owned_aircraft - held, s
    assert all(any(p is q for q in squadron.available_pilots) for p in landed_pilots)


def test_failed_restore_changes_nothing(
    game: Game, own: Game, start_miz: Path, tmp_path: Path
) -> None:
    with pytest.raises(RestoreError):
        write_restore(game, tmp_path / "no-tick.miz")
    own.dynamic_campaign = False
    with pytest.raises(RestoreError):
        write_restore(own, tmp_path / "not-dynamic.miz")
    own.dynamic_campaign = True

    squadron = red_squadron(own)
    ramp, landed = plan_by_hand(own, 2, squadron), plan_by_hand(own, 2, squadron)
    launch(own, ramp, "LIVE 1")
    launch(own, landed, "LIVE 2")
    own.live.landed.add("LIVE 2-1")
    tick_of(own, start_miz, launched=[ramp, landed])
    record = own.live
    marker, written_map = record.marker, record.written_map
    verdicts = dict(record.verdicts)
    packages = {c: list(c.ato.packages) for c in (own.blue, own.red)}
    flights = ato_flights(own)
    untasked = {s: s.untasked_aircraft for s in squadrons(own)}
    start_time = own.conditions.start_time
    planners = own.ground_planners
    blocker = tmp_path / "file"
    blocker.write_text("")

    with pytest.raises(OSError):
        write_restore(own, blocker / "retribution_dynamic.miz")

    assert record.marker == marker and record.written_map is written_map
    assert record.verdicts == verdicts
    assert {c: list(c.ato.packages) for c in (own.blue, own.red)} == packages
    assert ato_flights(own) == flights
    assert ramp.launched and ramp.live_group_name == "LIVE 1"
    assert landed.launched
    assert {s: s.untasked_aircraft for s in squadrons(own)} == untasked
    assert own.conditions.start_time == start_time
    assert own.ground_planners is planners


def test_failed_write_keeps_the_previous_mission(
    own: Game, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write failing half-way leaves the mission written before as it was,
    and no temporary file."""
    path = tmp_path / "retribution_dynamic.miz"
    path.write_bytes(b"the previous mission")
    marker = own.live.marker

    def half_written(mission: Mission, filename: Any = None) -> None:
        Path(filename).write_bytes(b"half a mission")
        raise OSError("disk full")

    monkeypatch.setattr(Mission, "save", half_written)
    with pytest.raises(OSError, match="disk full"):
        write_restore(own, path)

    assert path.read_bytes() == b"the previous mission"
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]
    assert own.live.marker == marker


def test_replaced_mission_is_not_followed(
    own: Game, start_miz: Path, tmp_path: Path
) -> None:
    record = own.live
    record.receive(
        EventBatch(
            entries=(
                LogEntry(10, "dead", "b1", 1, unit=LogRef("Tank-10")),
                LogEntry(11, "dead", "b1", 1, unit=LogRef("Tank-11")),
            ),
            through=11,
        )
    )

    result = write_restore(own, tmp_path / "retribution_dynamic.miz")

    later = snapshot_of(
        own, start_miz, at=own.conditions.start_time + timedelta(hours=1)
    )
    assert record.decide(later) is MissionVerdict.FOREIGN
    assert not record.following_own_mission
    assert not record.keep(later)
    assert record.ready() == []
    assert record.cursor == 11 and not record.has_held

    restored = snapshot_of(own, result.path, boot="b2", at=result.start)
    assert record.decide(restored) is MissionVerdict.OWN
    assert record.current_verdict is MissionVerdict.OWN, "followed"
    assert record.name_map is record.written_map
    assert record.keep(restored)


def test_restored_missions_tick_waits_for_its_snapshot(
    own: Game, tmp_path: Path
) -> None:
    """The tick and the name map belong to the same mission: once the
    restored mission is followed, the old mission's tick is gone until the
    restored one gives a snapshot that can be kept."""
    record = own.live
    old_tick = record.tick
    assert old_tick is not None

    write_restore(own, tmp_path / "retribution_dynamic.miz")
    assert record.tick is old_tick, "kept until the restored mission is followed"

    untimed = taken(envelope(own, "b2", 1, timed=False))
    assert record.decide(untimed) is MissionVerdict.OWN
    assert record.name_map is record.written_map
    assert not record.keep(untimed)
    assert own.live.tick is None

    timed = taken(envelope(own, "b2", 2))
    assert record.decide(timed) is MissionVerdict.OWN
    assert record.keep(timed)
    assert record.tick is not None
    assert (record.tick.boot, record.tick.minute) == ("b2", 2)


def test_restore_twice_writes_the_same_war(own: Game, tmp_path: Path) -> None:
    tick = own.live.tick

    def standing(path: Path) -> dict[str, tuple[float, float, float]]:
        name_map = own.live.written_map
        assert name_map is not None
        groups, statics = miz_objects(path)
        units = [u for g in groups for u in g["units"]] + list(statics)
        return {
            u["name"]: (u["x"], u["y"], u["hdg"])
            for u in units
            if u["name"] in name_map.theater_objects
        }

    first = write_restore(own, tmp_path / "first.miz")
    first_war = standing(first.path)
    first_marker = own.live.marker
    assert own.live.tick is tick
    second = write_restore(own, tmp_path / "second.miz")

    assert first_war and standing(second.path) == first_war
    assert own.live.marker != first_marker
    assert markers(read_miz(first.path)) != markers(read_miz(second.path))


# Ground placement: the restored mission's ground units, ships and convoys
# stand where the tick saw them.

#: A campaign at sea: frigates, shipping lanes between friendly bases, roads.
SEA = Path("resources/campaigns/battle_of_abu_dhabi.yaml")


@pytest.fixture(scope="module")
def sea_miz(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Where the sea campaign's Begin Campaign mission is written."""
    return tmp_path_factory.mktemp("sea") / "start.miz"


@pytest.fixture(scope="module")
def sea(saved_games: Path, sea_miz: Path) -> Game:
    """Begin Campaign's game of the sea campaign, its mission running: a blue
    frigate group sailing to a destination, a red convoy on the road and a
    blue cargo ship in its lane."""
    game = new_game(dynamic=True, squadrons_start_full=True, campaign_file=SEA)
    game.pass_turn(no_action=True)
    bandar, kish = base(game, "Bandar Abbas Intl"), base(game, "Kish Intl")
    frigates(game).target_position = bandar.shipping_lanes[kish][3]
    dhafra, maktoum = base(game, "Al Dhafra AFB"), base(game, "FOB Al Maktoum")
    red_types = list(dhafra.base.armor)
    game.red.transfers.new_transfer(
        TransferOrder(dhafra, maktoum, {red_types[0]: 2, red_types[-1]: 2}),
        game.conditions.start_time,
    )
    bandar.base.commission_units({red_types[0]: 4})
    game.blue.transfers.new_transfer(
        TransferOrder(bandar, kish, {red_types[0]: 4}), game.conditions.start_time
    )
    game.live.new_marker()
    sim = generate_starting_mission(game, sea_miz.parent)
    assert sim.unit_map is not None
    game.live.written(LiveUnitMap.from_generation(sim.unit_map, game))
    tick_of(game, sea_miz)
    return game


def base(game: Game, name: str) -> ControlPoint:
    return next(cp for cp in game.theater.controlpoints if cp.name == name)


def frigates(game: Game) -> ShipGroundObject:
    return next(
        tgo
        for tgo in base(game, "Bandar Abbas Intl").ground_objects
        if isinstance(tgo, ShipGroundObject)
    )


def restored_units(mission: Mission) -> dict[str, tuple[Any, Any]]:
    """Every unit of the mission's groups by name, with its group."""
    units: dict[str, tuple[Any, Any]] = {}
    for coalition in mission.coalition.values():
        for country in coalition.countries.values():
            groups: list[Any] = [
                *country.vehicle_group,
                *country.ship_group,
                *country.static_group,
            ]
            for group in groups:
                for unit in group.units:
                    units[str(unit.name)] = (unit, group)
    return units


def miz_units(miz: Path) -> dict[str, dict[str, Any]]:
    """The units of a written mission's groups by name, as a snapshot has
    them."""
    groups, statics = miz_objects(miz)
    return {u["name"]: u for g in groups for u in g["units"]} | {
        s["name"]: s for s in statics
    }


def at(point: Any) -> tuple[float, float]:
    return (point.x, point.y)


def near(a: tuple[float, float], b: tuple[float, float], within: float = 0.01) -> bool:
    return math.dist(a, b) <= within


def test_ground_units_stand_where_the_tick_saw_them(
    sea: Game, sea_miz: Path, tmp_path: Path
) -> None:
    own = copy(sea)
    name_map = own.live.name_map
    groups, _ = miz_objects(sea_miz)
    tgo_group = next(
        g
        for g in groups
        if g["cat"] == 2
        and len(g["units"]) > 1
        and all(u["name"] in name_map.theater_objects for u in g["units"])
    )
    first = tgo_group["units"][0]
    moved = {first["name"]: (first["x"] + 100.0, first["y"], 90.0)}
    ships = frigates(own)
    ship_names = [u.unit_name for g in ships.groups for u in g.units]
    start = miz_units(sea_miz)
    for name in ship_names:
        moved[name] = (start[name]["x"] + 1000.0, start[name]["y"] + 500.0, 45.0)
    tick_of(own, sea_miz, moved=moved)

    result = write_restore(own, tmp_path / "retribution_dynamic.miz")

    units = restored_units(read_miz(result.path))
    unit, group = units[first["name"]]
    assert near(at(unit.position), moved[first["name"]][:2])
    assert unit.heading == pytest.approx(90.0)
    assert len(group.points) == 1
    assert near(at(group.points[0].position), at(unit.position))
    for name in ship_names[1:]:
        assert near(at(units[name][0].position), moved[name][:2])
    lead, ship_group = units[ship_names[0]]
    assert near(at(lead.position), moved[ship_names[0]][:2])
    assert near(at(ship_group.points[0].position), at(lead.position))
    assert ships.target_position is not None
    assert len(ship_group.points) == 2, "the route after point 0 kept"
    assert near(at(ship_group.points[1].position), at(ships.target_position))
    assert result.unplaced == 0


#: Front-line unit classes whose every stance's orders start with a reform
#: point 50 m ahead of the group (artillery's depend on a target in range).
MANOEUVRING = {UnitClass.TANK, UnitClass.IFV, UnitClass.APC, UnitClass.ATGM}


def test_front_line_from_the_tick(own: Game, start_miz: Path, tmp_path: Path) -> None:
    name_map = own.live.name_map
    enemy = {
        cp.id: other
        for front in own.theater.conflicts()
        for cp, other in ((front.blue_cp, front.red_cp), (front.red_cp, front.blue_cp))
    }
    groups, _ = miz_objects(start_miz)
    moved: dict[str, tuple[float, float, float]] = {}
    #: The tick's positions per key: (x, y) and the old group's name.
    seen: dict[tuple[str, Any], list[tuple[tuple[float, float], str]]] = {}
    old_size: Counter[str] = Counter()
    for g in groups:
        for u in g["units"]:
            unit = name_map.front_line_units.get(u["name"])
            if unit is None:
                continue
            to = enemy[unit.origin.id].position
            d = math.dist((u["x"], u["y"]), at(to))
            x = u["x"] + 300.0 * (to.x - u["x"]) / d
            y = u["y"] + 300.0 * (to.y - u["y"]) / d
            moved[u["name"]] = (x, y, u["hdg"])
            key = (unit.unit_type.variant_id, unit.origin.id)
            seen.setdefault(key, []).append(((x, y), g["name"]))
            old_size[g["name"]] += 1
    assert moved, "the campaign has a front line"
    tick_of(own, start_miz, moved=moved)

    result = write_restore(own, tmp_path / "retribution_dynamic.miz")

    written = own.live.written_map
    assert written is not None
    mission = read_miz(result.path)
    placed_from: Counter[str] = Counter()
    new_groups = ordered = 0
    for coalition in mission.coalition.values():
        for country in coalition.countries.values():
            for group in country.vehicle_group:
                front = [written.front_line_units.get(str(u.name)) for u in group.units]
                if front[0] is None:
                    continue
                new_groups += 1
                previous: Optional[str] = None
                for vehicle, mapped in zip(group.units, front):
                    assert mapped is not None
                    key = (mapped.unit_type.variant_id, mapped.origin.id)
                    old = next(
                        (o for xy, o in seen[key] if near(xy, at(vehicle.position))),
                        None,
                    )
                    assert old is not None, f"{vehicle.name} not on a tick position"
                    if previous is not None and old != previous:
                        assert (
                            placed_from[previous] == old_size[previous]
                        ), "an old group split while the new one had room"
                    placed_from[old] += 1
                    previous = old
                first = group.units[0].position
                assert near(at(group.points[0].position), at(first))
                if front[0].unit_type.unit_class not in MANOEUVRING:
                    continue
                ordered += 1
                assert len(group.points) >= 2, "the stance's orders"
                assert math.dist(
                    at(group.points[0].position), at(group.points[1].position)
                ) == pytest.approx(50.0, abs=1.0), "the reform point ahead of it"
    assert new_groups and ordered
    assert sum(placed_from.values()) == len(moved)


def test_convoy_rejoins_its_route(sea: Game, sea_miz: Path, tmp_path: Path) -> None:
    own = copy(sea)
    [convoy] = list(own.red.transfers.convoys)
    route = list(convoy.origin.convoy_route_to(convoy.destination))
    assert len(route) > 3
    names = [n for n, u in own.live.name_map.convoys.items() if u.convoy is convoy]
    third = route[2]
    moved = {
        name: (third.x + 30.0 - 15.0 * i, third.y + 30.0, 10.0 * i)
        for i, name in enumerate(names)
    }
    tick_of(own, sea_miz, moved=moved)
    by_type: dict[str, list[tuple[float, float]]] = {}
    for name in names:
        by_type.setdefault(
            own.live.name_map.convoys[name].unit_type.variant_id, []
        ).append(moved[name][:2])

    result = write_restore(own, tmp_path / "retribution_dynamic.miz")

    written = own.live.written_map
    assert written is not None
    units = restored_units(read_miz(result.path))
    new_names = [n for n, u in written.convoys.items() if u.convoy is convoy]
    assert len(new_names) == len(names)
    lead, group = units[new_names[0]]
    assert near(at(lead.position), moved[names[0]][:2])
    for name in new_names:
        kind = written.convoys[name].unit_type.variant_id
        assert any(near(at(units[name][0].position), xy) for xy in by_type[kind])
    assert near(at(group.points[0].position), at(lead.position))
    later = [at(p.position) for p in group.points[1:]]
    assert len(later) == len(route) - 2, "the points before the nearest dropped"
    assert all(near(a, at(b)) for a, b in zip(later, route[2:]))
    assert near(later[-1], at(convoy.route_end))


def test_cargo_ship_rejoins_its_lane(sea: Game, sea_miz: Path, tmp_path: Path) -> None:
    own = copy(sea)
    [ship] = list(own.blue.transfers.cargo_ships)
    lane = list(ship.route)
    [name] = [n for n, s in own.live.name_map.cargo_ships.items() if s is ship]
    fourth = lane[3]
    tick_of(own, sea_miz, moved={name: (fourth.x + 50.0, fourth.y - 50.0, 200.0)})

    result = write_restore(own, tmp_path / "retribution_dynamic.miz")

    written = own.live.written_map
    assert written is not None
    [new_name] = [n for n, s in written.cargo_ships.items() if s is ship]
    unit, group = restored_units(read_miz(result.path))[new_name]
    assert near(at(unit.position), (fourth.x + 50.0, fourth.y - 50.0))
    assert near(at(group.points[0].position), at(unit.position))
    later = [at(p.position) for p in group.points[1:]]
    assert len(later) == len(lane) - 3, "the points before the nearest dropped"
    assert all(near(a, at(b)) for a, b in zip(later, lane[3:]))
    assert near(later[-1], at(lane[-1]))


def test_tick_and_game_disagree(own: Game, start_miz: Path, tmp_path: Path) -> None:
    """Review Focus 2: the tick never brings the dead back nor loses the
    living; a name it has that the map does not know is ignored."""
    name_map = own.live.name_map
    groups, _ = miz_objects(start_miz)
    tgo_groups = [
        g
        for g in groups
        if g["cat"] == 2
        and len(g["units"]) > 2
        and all(u["name"] in name_map.theater_objects for u in g["units"])
    ]
    gone = tgo_groups[0]["units"][1]["name"]
    killed = tgo_groups[1]["units"][1]
    gone_unit = name_map.theater_objects[gone].theater_unit
    apply_entries(
        own,
        [
            LogEntry(
                1,
                "dead",
                "b1",
                1,
                unit=LogRef(killed["name"], killed["type"], 1, "ground"),
                pos=LogPos(killed["x"], killed["y"], 0.0),
            )
        ],
    )
    assert not name_map.theater_objects[killed["name"]].theater_unit.alive
    tick_of(
        own,
        start_miz,
        gone={gone},
        extra=[tank("Stranger", 1000.0, -2000.0)],
        moved={killed["name"]: (killed["x"] + 40.0, killed["y"], 0.0)},
    )

    result = write_restore(own, tmp_path / "retribution_dynamic.miz")

    mission = read_miz(result.path)
    units = restored_units(mission)
    assert near(at(units[gone][0].position), at(gone_unit.position))
    assert result.unplaced >= 1
    assert killed["name"] not in units
    wrecks = [
        g.units[0]
        for coalition in mission.coalition.values()
        for country in coalition.countries.values()
        for g in country.static_group
        if g.dead
    ]
    assert any(
        w.type == killed["type"] and near(at(w.position), (killed["x"], killed["y"]))
        for w in wrecks
    ), "its wreck where it died"
    assert "Stranger-1" not in units


def test_normal_campaign_generation_unchanged(
    saved_games: Path, tmp_path: Path
) -> None:
    """Without a restore, a normal campaign's mission has a group per flight
    and parks each squadron's untasked aircraft, base by base until one runs
    out of parking (a regression guard: it passed before restores existed)."""
    game = new_game(dynamic=False, squadrons_start_full=True)
    game.pass_turn(no_action=True)
    flights = ato_flights(game)
    assert flights
    untasked = {s: s.untasked_aircraft for s in squadrons(game)}
    path = tmp_path / "normal.miz"
    MissionSimulation(game).begin_simulation()  # as Begin Campaign's does

    unit_map = MissionGenerator(game, game.conditions.start_time).generate_miz(path)

    by_unit = {str(u.name): g for g in aircraft_groups(read_miz(path)) for u in g.units}
    for flight in flights:
        names = [n for n, u in unit_map.aircraft.items() if u.flight is flight]
        assert len(names) == flight.count, flight
        [group] = {id(by_unit[n]): by_unit[n] for n in names}.values()
        assert len(group.units) == flight.count, flight
    planned = {id(f) for f in flights}
    parked = Counter(
        u.flight.squadron
        for u in unit_map.aircraft.values()
        if id(u.flight) not in planned
    )
    for cp in game.theater.controlpoints:
        short = not isinstance(cp, (Airfield, Fob))
        for s in cp.squadrons:
            if short:
                assert parked[s] == 0, s
                continue
            assert parked[s] <= untasked[s], s
            short = parked[s] < untasked[s]
    assert sum(parked.values()) > 0
