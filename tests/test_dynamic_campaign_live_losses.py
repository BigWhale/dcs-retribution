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
from types import SimpleNamespace
from typing import Any, Callable, cast

import dcs.terrain
import pytest
from dcs import Mission
from dcs.mapping import Point
from dcs.terrain.terrain import Terrain

from game import persistency
from game.ato.flight import Flight
from game.ato.flighttype import FlightType
from game.ato.package import Package
from game.ato.starttype import StartType
from game.companionlink.eventlog import EventBatch, LogEntry, LogPos, LogRef
from game.companionlink.link import LinkStatus
from game.companionlink.livelosses import apply_entries, finish_flight, settle
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
from game.missiongenerator.companionmarker import add_mission_marker
from game.sim.missionsimulation import MissionSimulation
from game.squadrons import Pilot, Squadron
from game.squadrons.pilot import PilotStatus
from game.theater import (
    Airfield,
    Carrier,
    ControlPoint,
    Lha,
    OffMapSpawn,
    Player,
    TheaterUnit,
)
from game.transfers import CargoShip, Convoy, TransferOrder
from tests.test_dynamic_campaign_air_wing import (
    generate_starting_mission,
    new_game,
    plan_by_hand,
    saved_games,  # noqa: F401 (the fixture)
    squadrons,
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
    assert within_bounds(group.units[0].position, mission.terrain)
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


def within_bounds(position: Point, terrain: Terrain) -> bool:
    """Inside the terrain's bounds, whichever way round pydcs gives them."""
    b = terrain.bounds
    return min(b.top, b.bottom) < position.x < max(b.top, b.bottom) and min(
        b.left, b.right
    ) < position.y < max(b.left, b.right)


#: Every terrain pydcs ships; each makes itself with no arguments.
TERRAINS: list[Callable[[], Terrain]] = [
    t
    for t in vars(dcs.terrain).values()
    if isinstance(t, type) and issubclass(t, Terrain) and t is not Terrain
]


@pytest.mark.parametrize("terrain_type", TERRAINS, ids=lambda t: t.__name__)
def test_marker_stands_inside_every_terrain(
    terrain_type: Callable[[], Terrain],
) -> None:
    terrain = terrain_type()
    mission = Mission(terrain)
    usa = mission.country("USA")
    game = SimpleNamespace(
        theater=SimpleNamespace(terrain=terrain),
        blue=SimpleNamespace(faction=SimpleNamespace(country=usa)),
    )

    add_mission_marker(mission, cast(Game, game), "uuid")

    [group] = usa.static_group
    assert str(group.units[0].name) == MARKER_PREFIX + "uuid"
    assert within_bounds(group.units[0].position, terrain)


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


def test_cursor_never_passes_entries_handed_out() -> None:
    """A dropped entry after own ones waits until they are applied, so a save
    in between never moves the cursor past an entry not applied yet."""
    record = LiveRecord()
    marker = MARKER_PREFIX + record.new_marker()
    record.written(LiveUnitMap())
    record.receive(EventBatch((), through=10))
    record.decide(snapshot("own", 1, marker))
    record.decide(snapshot("other", 1))
    record.receive(
        EventBatch(
            (
                entry(11, "own", 1),
                entry(12, "own", 1),
                entry(13, "other", 1),
                entry(14, "own", 1),
                entry(15, "own", 1),
            ),
            through=15,
        )
    )

    assert [e.seq for e in record.ready()] == [11, 12]
    assert record.cursor == 10
    record.applied(12)
    assert [e.seq for e in record.ready()] == [14, 15]
    assert record.cursor == 13
    record.applied(15)
    assert record.cursor == 15

    record.receive(
        EventBatch(tuple(entry(s, "own", 1) for s in (16, 17, 18)), through=18)
    )
    assert [e.seq for e in record.ready(limit=2)] == [16, 17]
    record.applied(17)
    assert [e.seq for e in record.ready(limit=2)] == [18]


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


# Losses applied: entries of the campaign's own mission, applied as a turn
# applies a mission's results. Each test mutates its game, so each makes one.

BOOT, SESSION = "boot", 1


@dataclass
class Live:
    game: Game
    flight: Flight  # launched, as "LIVE 1-1" and "LIVE 1-2"


@pytest.fixture
def live(saved_games: Path, tmp_path: Path) -> Live:
    """Begin Campaign's mission accepted, a flight planned since launched."""
    game = new_game(dynamic=True, squadrons_start_full=True, campaign_file=CAMPAIGN)
    game.pass_turn(no_action=True)
    uuid = game.live.new_marker()
    sim = generate_starting_mission(game, tmp_path)
    assert sim.unit_map is not None
    game.live.written(LiveUnitMap.from_generation(sim.unit_map, game))
    verdict = game.live.decide(snapshot(BOOT, SESSION, MARKER_PREFIX + uuid))
    assert verdict is MissionVerdict.OWN
    flight = plan_by_hand(game, 2)
    flight.launched = True
    flight.live_group_name = "LIVE 1"
    game.live.name_map.add_launched(flight, ["LIVE 1-1", "LIVE 1-2"])
    return Live(game, flight)


def log(
    seq: int,
    kind: str,
    name: str = "",
    other: str = "",
    *,
    category: str = "",
    type: str = "",
    player: str = "",
    place: str = "",
    side: int | None = None,
    pos: LogPos | None = None,
) -> LogEntry:
    return LogEntry(
        seq,
        kind,
        BOOT,
        SESSION,
        unit=LogRef(name, type, None, category, "", player) if name else None,
        other=LogRef(other) if other else None,
        place=place,
        side=side,
        pos=pos,
    )


def tgo_units(game: Game) -> list[tuple[str, TheaterUnit]]:
    return [
        (name, m.theater_unit) for name, m in game.live.name_map.theater_objects.items()
    ]


def ato_flights(game: Game, player: Player) -> list[Flight]:
    ato = game.coalition_for(player).ato
    return [f for p in ato.packages for f in p.flights]


def test_tgo_unit_killed(live: Live) -> None:
    name, unit = next((n, u) for n, u in tgo_units(live.game) if u.alive)

    batch = apply_entries(live.game, [log(1, "dead", name, category="ground")])

    assert unit.alive is False
    assert unit.ground_object in batch.events.updated_tgos
    assert batch.dead == [name]
    assert batch.entries == 1


def test_repeated_deaths_count_once_across_reload(live: Live, tmp_path: Path) -> None:
    """Review Focus 1: one death told three times costs one aircraft, one pilot."""
    squadron = live.flight.squadron
    owned, destroyed = squadron.owned_aircraft, squadron.destroyed_aircraft

    apply_entries(live.game, [log(1, "dead", "LIVE 1-1", category="plane")])
    live.game.savepath = str(tmp_path / "live.retribution")
    assert persistency.save_game(live.game)
    game = persistency.load_game(live.game.savepath)
    assert game is not None
    Migrator(game, is_liberation=False)
    apply_entries(game, [log(2, "crash", "LIVE 1-1", category="plane")])
    # The killer is not a loss; only its victim.
    apply_entries(game, [log(3, "kill", "LIVE 1-2", "LIVE 1-1")])

    [flight] = [f for f in ato_flights(game, Player.BLUE) if f.id == live.flight.id]
    squadron = flight.squadron
    assert squadron.owned_aircraft == owned - 1
    assert squadron.destroyed_aircraft == destroyed + 1
    first, *rest = [p for p in flight.roster.iter_pilots() if p is not None]
    assert first.status is PilotStatus.Dead
    assert [p.status for p in rest] == [PilotStatus.Active]
    assert [p for p in squadron.current_roster if not p.alive] == [first]


def test_flight_finished_when_rest_landed(live: Live) -> None:
    game, flight = live.game, live.flight
    squadron = flight.squadron
    package = flight.package

    batch = apply_entries(
        game,
        [
            log(1, "dead", "LIVE 1-1", category="plane"),
            log(2, "land", "LIVE 1-2", category="plane", place="Ramat David"),
        ],
    )

    assert batch.finished == [flight]
    assert squadron in batch.squadrons
    assert flight in package.flights, "returned, not removed"
    finish_flight(game, flight)
    settle(game, batch)
    assert flight not in ato_flights(game, Player.BLUE)
    assert package not in game.blue.ato.packages
    held = sum(
        f.count for f in ato_flights(game, Player.BLUE) if f.squadron is squadron
    )
    assert squadron.untasked_aircraft == squadron.owned_aircraft - held


def test_parked_aircraft_destroyed(live: Live) -> None:
    game = live.game
    name, parked = next(
        (n, u)
        for n, u in game.live.name_map.aircraft.items()
        if u.pilot is None and u.flight.squadron.player.is_red
    )
    squadron = parked.flight.squadron
    owned = squadron.owned_aircraft
    pilots = list(squadron.available_pilots)

    batch = apply_entries(game, [log(1, "dead", name, category="plane")])
    settle(game, batch)

    assert squadron.owned_aircraft == owned - 1
    assert squadron.available_pilots == pilots
    assert all(p.alive for p in squadron.current_roster)
    assert batch.finished == []


def isolated_circle(game: Game) -> ZoneShape:
    zones = [
        cast(ZoneShape, m.trigger_zone)
        for m in game.live.name_map.scenery_objects.values()
    ]
    return next(
        z
        for z in zones
        if z.corners is None
        and all(o is z or math.hypot(o.x - z.x, o.y - z.y) > 1000 for o in zones)
    )


def test_scenery_death_by_position(live: Live) -> None:
    game = live.game
    zone = isolated_circle(game)
    scenery = game.live.name_map.scenery_objects[zone.name].ground_unit
    alive = scenery.alive

    batch = apply_entries(
        game,
        [
            log(
                1,
                "dead",
                "123456",
                category="scenery",
                pos=LogPos(zone.x + 10, zone.y, 0),
            )
        ],
    )

    assert (alive, scenery.alive) == (True, False)
    assert batch.dead == [zone.name]


def plan_red(game: Game, squadron: Squadron) -> Flight:
    package = Package(squadron.location, game.db.flights, auto_asap=True)
    flight = Flight(package, squadron, 1, FlightType.BARCAP, StartType.COLD, None)
    package.add_flight(flight)
    flight.recreate_flight_plan()
    package.set_tot_asap(game.conditions.start_time)
    game.red.ato.add_package(package)
    return flight


def test_capture_applied(live: Live) -> None:
    game = live.game
    cp, squadron = next(
        (cp, s)
        for cp in game.theater.controlpoints
        if isinstance(cp, Airfield) and cp.captured.is_red
        for s in cp.squadrons
        if s.untasked_aircraft > 0
    )
    stranded = plan_red(game, squadron)

    batch = apply_entries(
        game, [log(1, "base_captured", place=cp.airport.name, side=2)]
    )

    assert cp.captured is Player.BLUE
    assert batch.captured == [cp]
    assert batch.threat_changed
    assert batch.stranded == [stranded]
    assert stranded.squadron in batch.squadrons
    assert cp in batch.events.updated_control_points


def test_unknown_names_change_nothing(
    live: Live, caplog: pytest.LogCaptureFixture
) -> None:
    """Review Focus 3: names the map does not know are ignored, quietly."""
    game = live.game
    owned = [s.owned_aircraft for s in squadrons(game)]
    alive = [u.alive for _, u in tgo_units(game)]
    unknown = [
        log(1, "dead", "dcs-companion-mission|x", category="static"),
        log(2, "kill", "Enemy 1-1", "CTLD crate 1"),
        log(3, "vanished", "Stranger 1-1", category="plane"),
        log(4, "dead", "CTLD crate 1", category="static", pos=LogPos(1000, 2000, 0)),
    ]

    with caplog.at_level(logging.DEBUG):
        batch = apply_entries(game, unknown)

    assert [s.owned_aircraft for s in squadrons(game)] == owned
    assert [u.alive for _, u in tgo_units(game)] == alive
    assert batch.dead == [] and game.live.dead == set()
    assert not batch.squadrons and not batch.threat_changed
    for name in ("dcs-companion-mission|x", "CTLD crate 1", "Stranger 1-1"):
        assert name not in caplog.text


def test_vanished_after_landing_is_not_a_loss(live: Live) -> None:
    """Review Focus 4: a parked aircraft DCS removed, or a player's, is not lost."""
    game = live.game
    squadron = live.flight.squadron
    owned = squadron.owned_aircraft

    apply_entries(game, [log(1, "land", "LIVE 1-2", category="plane")])
    apply_entries(game, [log(2, "vanished", "LIVE 1-2", category="plane")])
    assert squadron.owned_aircraft == owned

    apply_entries(
        game, [log(3, "vanished", "LIVE 1-1", category="plane", player="Uzi")]
    )
    assert squadron.owned_aircraft == owned

    batch = apply_entries(game, [log(4, "vanished", "LIVE 1-1", category="plane")])
    assert squadron.owned_aircraft == owned - 1
    assert batch.dead == ["LIVE 1-1"]


def test_uncapturable_captures_skipped(saved_games: Path) -> None:
    """Review Focus 5's capture rules: carriers, LHAs, off-map spawns, a base
    already the side's and a place Retribution does not know are skipped."""
    game = new_game(
        dynamic=True,
        squadrons_start_full=True,
        campaign_file=Path("resources/campaigns/black_sea.yaml"),
    )
    points = game.theater.controlpoints
    carrier = next(
        cp for cp in points if isinstance(cp, Carrier) and cp.captured.is_red
    )
    off_map = next(
        cp for cp in points if isinstance(cp, OffMapSpawn) and cp.captured.is_red
    )
    lha = next(cp for cp in points if isinstance(cp, Lha) and cp.captured.is_blue)
    blue = next(cp for cp in points if isinstance(cp, Airfield) and cp.captured.is_blue)
    owners = [cp.captured for cp in points]

    batch = apply_entries(
        game,
        [
            log(1, "base_captured", place=carrier.name, side=2),
            log(2, "base_captured", place=off_map.name, side=2),
            log(3, "base_captured", place=lha.name, side=1),
            log(4, "base_captured", place=blue.airport.name, side=2),
            log(5, "base_captured", place="Nowhere", side=2),
        ],
    )

    assert batch.captured == []
    assert [cp.captured for cp in points] == owners
    assert not batch.threat_changed


def test_wreck_recorded(live: Live) -> None:
    game = live.game
    (sam, _), (other, _) = tgo_units(game)[:2]
    before = len(game.get_destroyed_units())

    apply_entries(
        game,
        [
            log(
                1,
                "dead",
                sam,
                category="ground",
                type="SA-11 Buk LN 9A310M1",
                pos=LogPos(1000, 2000, 5),
            ),
            log(
                2,
                "dead",
                other,
                category="ground",
                type="F-16C_CRASH",
                pos=LogPos(1000, 2000, 5),
            ),
        ],
    )

    wrecks = game.get_destroyed_units()[before:]
    assert wrecks == [
        {
            "x": 1000.0,
            "y": 5.0,
            "z": 2000.0,
            "type": "SA-11 Buk LN 9A310M1",
            "orientation": 0.0,
        }
    ]


def test_wreck_once_per_name(live: Live) -> None:
    """Every death leaves a wreck, the map's units or not, once per name."""
    game = live.game
    sam, _ = next((n, u) for n, u in tgo_units(game) if u.alive)
    before = len(game.get_destroyed_units())

    def death(seq: int, kind: str, name: str, x: float) -> LogEntry:
        return log(
            seq, kind, name, category="ground", type="T-72B", pos=LogPos(x, 2000, 0)
        )

    apply_entries(
        game,
        [
            death(1, "dead", "Script tank 1", 1000),
            death(2, "crash", "Script tank 1", 1100),
            death(3, "dead", sam, 1200),
        ],
    )
    apply_entries(game, [death(4, "crash", sam, 1300)])

    wrecks = game.get_destroyed_units()[before:]
    assert [w["x"] for w in wrecks] == [1000.0, 1200.0]
    assert "Script tank 1" not in game.live.dead
