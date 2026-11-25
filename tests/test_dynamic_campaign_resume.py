"""A dynamic campaign's resumed flights: which aircraft of a flight in the air
at the tick resume, and the waypoint they fly on to.

A launched flight's aircraft resume when the tick has them in the air, flown
by the AI and not counted dead; the lead is the first of them. The waypoint
flown to ends the route leg nearest the lead; legs within `TIE_M` of the
nearest are told apart by their planned times.

The flights are hand-planned BARCAPs: takeoff (0), the racetrack's start (1)
and end (2), the landing (3). The tests that need a route of a known shape
lay it out from the takeoff waypoint, so they do not depend on the theater.

A restore writes the flights in the air at the tick into the restored mission
(read back here with pydcs): each resumed aircraft where the tick saw it, with
its fuel and the weapons it still carries, flying on to the waypoint it was
flying to; the rest of its flight goes back to its squadron.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest
from dcs.action import DoScript
from dcs.mapping import Point
from dcs.mission import Mission
from dcs.unitgroup import FlyingGroup

from game.ato.flight import Flight
from game.ato.flighttype import FlightType
from game.ato.flightwaypointtype import FlightWaypointType
from game.ato.loadouts import Loadout
from game.ato.package import Package
from game.ato.starttype import StartType
from game.companionlink.restore import RestoreResult, write_restore
from game.companionlink.resume import (
    TIE_M,
    Resumed,
    ResumedUnit,
    flown_to_index,
    plan_resume,
)
from game.data.weapons import Weapon
from game.game import Game
from game.squadrons import Squadron
from game.theater import Airfield
from game.utils import meters
from tests.conftest import plan_by_hand, squadrons
from tests.test_dynamic_campaign_restore import (  # noqa: F401
    aircraft_groups,
    ato_flights,
    begun,
    launch,
    own,
    parked_per_squadron,
    read_miz,
    red_squadron,
    running,
    start_miz,
    tank,
    tick_of,
)

#: The route laid out by `lay_out`: the racetrack's start, its end, and the
#: landing back at the takeoff, in km north and east of the takeoff.
START_KM = (80.0, 0.0)
END_KM = (40.0, 20.0)


def along(a: Point, b: Point, t: float) -> tuple[float, float]:
    """The point a fraction `t` of the way from `a` to `b`, as (x, y)."""
    return a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t


def lay_out(flight: Flight) -> list[Point]:
    """Moves the racetrack to START_KM and END_KM from the takeoff: the way
    out runs north, the way back comes in at an angle, so the two run close
    together near the field only. The waypoints' positions, in order."""
    waypoints = flight.flight_plan.waypoints
    kinds = [w.waypoint_type for w in waypoints[:4]]
    assert kinds == [
        FlightWaypointType.TAKEOFF,
        FlightWaypointType.PATROL_TRACK,
        FlightWaypointType.PATROL,
        FlightWaypointType.LANDING_POINT,
    ]
    field = waypoints[0].position
    for waypoint, (north, east) in zip(waypoints[1:3], (START_KM, END_KM)):
        waypoint.position = field.new_in_same_map(
            field.x + north * 1000, field.y + east * 1000
        )
    waypoints[3].position = field.new_in_same_map(field.x, field.y)
    return [w.position for w in waypoints]


def flying(x: float, y: float, **extra: Any) -> dict[str, Any]:
    """A tick unit's fields for an aircraft in the air at (x, y)."""
    return {"x": x, "y": y, "inAir": True, **extra}


def test_tick_unit_fuel_and_ammo(own: Game, start_miz: Path) -> None:
    flight = plan_by_hand(own, 2, red_squadron(own))
    launch(own, flight, "LIVE 1")
    armor = tank("Armor", 1000.0, -2000.0)
    armor["units"][0]["ammo"] = [["weapons.shells.2A46M_125_HE", 20]]
    armor["units"][0]["fuel"] = 1.0
    tick = tick_of(
        own,
        start_miz,
        launched=[flight],
        extra=[armor],
        air={
            "LIVE 1-1": {
                "fuel": 0.734,
                "ammo": [
                    ["weapons.missiles.AIM_120C", 2],
                    ["weapons.missiles.AIM_9X"],
                    ["weapons.missiles.AIM_9X", 1.5],
                    [7, 2],
                ],
            }
        },
    )
    units = {u.name: u for u in tick.units}

    assert units["LIVE 1-1"].fuel == 0.734
    assert units["LIVE 1-1"].ammo == (("weapons.missiles.AIM_120C", 2),)
    assert units["LIVE 1-2"].fuel is None and units["LIVE 1-2"].ammo is None
    assert units["Armor-1"].fuel is None and units["Armor-1"].ammo is None


def test_resumed_members_by_tick(own: Game, start_miz: Path) -> None:
    flight = plan_by_hand(own, 3, red_squadron(own))
    launch(own, flight, "LIVE 1")
    own.live.dead.add("LIVE 1-2")
    waypoints = flight.flight_plan.waypoints
    x, y = along(waypoints[1].position, waypoints[2].position, 0.5)
    tick = tick_of(
        own,
        start_miz,
        launched=[flight],
        air={
            "LIVE 1-1": flying(x, y, alt=6000.0, hdg=270.0, spd=220.0, fuel=0.5),
            "LIVE 1-3": {"inAir": False},
        },
    )
    assert tick.abs_time is not None

    plan = plan_resume(own, flight, tick, tick.abs_time)

    assert plan is not None
    assert plan.members == [list(flight.iter_members())[0]]
    state = plan.state
    assert isinstance(state, Resumed)
    assert state.units == (
        ResumedUnit(
            name="LIVE 1-1",
            x=x,
            y=y,
            alt=6000.0,
            heading=270.0,
            speed=220.0,
            fuel=0.5,
            ammo=None,
        ),
    )
    assert state.planned_loadout == []
    position = state.estimate_position()
    assert (position.x, position.y) == (x, y)
    assert state.estimate_altitude() == (meters(6000), "BARO")
    assert state.estimate_speed().meters_per_second == 220
    assert state.estimate_fuel() == 0.5 * flight.unit_type.dcs_unit_type.fuel_max


def test_resumed_none_in_air(own: Game, start_miz: Path) -> None:
    flight = plan_by_hand(own, 2, red_squadron(own))
    launch(own, flight, "LIVE 1")
    tick = tick_of(own, start_miz, launched=[flight])
    assert tick.abs_time is not None

    assert plan_resume(own, flight, tick, tick.abs_time) is None


def test_player_unit_not_resumed(own: Game, start_miz: Path) -> None:
    flight = plan_by_hand(own, 2, red_squadron(own))
    launch(own, flight, "LIVE 1")
    waypoints = flight.flight_plan.waypoints
    x, y = along(waypoints[1].position, waypoints[2].position, 0.5)
    tick = tick_of(
        own,
        start_miz,
        launched=[flight],
        air={"LIVE 1-1": flying(x, y, player="Pilot")},
    )
    assert tick.abs_time is not None

    assert plan_resume(own, flight, tick, tick.abs_time) is None


def test_flown_to_nearest_leg(own: Game, start_miz: Path) -> None:
    """The way back is nearest; the tick's time, on the racetrack's, does not
    count when no other leg is as near."""
    flight = plan_by_hand(own, 2, red_squadron(own))
    launch(own, flight, "LIVE 1")
    points = lay_out(flight)
    x, y = along(points[2], points[3], 0.4)
    plan = flight.flight_plan
    at = plan.tot_for_waypoint(plan.waypoints[1])
    assert at is not None
    at += timedelta(minutes=10)
    tick = tick_of(own, start_miz, launched=[flight], air={"LIVE 1-1": flying(x, y)})

    assert flown_to_index(flight, x, y, at) == 3
    resumed = plan_resume(own, flight, tick, at)
    assert resumed is not None
    assert resumed.state.waypoint_index == 2
    assert resumed.state.current_waypoint is plan.waypoints[2]


def test_flown_to_orbit(own: Game, start_miz: Path) -> None:
    flight = plan_by_hand(own, 2, red_squadron(own))
    launch(own, flight, "LIVE 1")
    points = lay_out(flight)
    x, y = along(points[1], points[2], 0.5)
    plan = flight.flight_plan
    begins = plan.tot_for_waypoint(plan.waypoints[1])
    ends = plan.depart_time_for_waypoint(plan.waypoints[2])
    assert begins is not None and ends is not None
    at = begins + (ends - begins) / 2
    tick = tick_of(own, start_miz, launched=[flight], air={"LIVE 1-1": flying(x, y)})

    assert flown_to_index(flight, x, y, at) == 2
    resumed = plan_resume(own, flight, tick, at)
    assert resumed is not None
    assert resumed.state.current_waypoint is plan.waypoints[1]


def test_flown_to_landing(own: Game, start_miz: Path) -> None:
    flight = plan_by_hand(own, 2, red_squadron(own))
    points = lay_out(flight)
    x, y = along(points[2], points[3], 0.9)
    plan = flight.flight_plan
    ends = plan.depart_time_for_waypoint(plan.waypoints[2])
    assert ends is not None

    index = flown_to_index(flight, x, y, ends + timedelta(minutes=5))

    assert index == 3
    assert plan.waypoints[index].waypoint_type is FlightWaypointType.LANDING_POINT


def test_flown_to_late_flight_on_shared_line(own: Game) -> None:
    """Near the field the way out and the way back run within TIE_M of each
    other: the planned times tell them apart, but a flight behind its
    schedule is never moved ahead of where it is."""
    flight = plan_by_hand(own, 2, red_squadron(own))
    points = lay_out(flight)
    plan = flight.flight_plan
    takeoff = plan.takeoff_time()
    off_station = plan.depart_time_for_waypoint(plan.waypoints[2])
    assert off_station is not None
    near_field = along(points[0], points[1], 0.1)
    assert _to_leg(near_field, points[2], points[3]) < TIE_M, "both legs near"

    # On time, on the way out; on time, on the way back.
    assert flown_to_index(flight, *near_field, takeoff + timedelta(minutes=1)) == 1
    assert flown_to_index(flight, *near_field, off_station + timedelta(minutes=1)) == 3

    # Late, past every planned time: where it is decides.
    late = off_station + timedelta(hours=10)
    assert flown_to_index(flight, *near_field, late) == 1
    halfway_out = along(points[0], points[1], 0.5)
    assert _to_leg(halfway_out, points[2], points[3]) > TIE_M
    assert _to_leg(halfway_out, points[1], points[2]) > TIE_M
    assert flown_to_index(flight, *halfway_out, late) == 1


def _to_leg(p: tuple[float, float], a: Point, b: Point) -> float:
    """The distance from p to the segment a-b, in map metres (the test's own
    measure, to check the layout)."""
    ax, ay, bx, by = a.x, a.y, b.x, b.y
    dx, dy = bx - ax, by - ay
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


# The restored mission: the resumed flights written into it.

#: The tick unit fields of the flight in the air on its way to the racetrack.
FUEL = (0.6, 0.4)


def restore(game: Game, tmp_path: Path) -> tuple[RestoreResult, Mission]:
    result = write_restore(game, tmp_path / "retribution_dynamic.miz")
    return result, read_miz(result.path)


def written_group(mission: Mission, flight: Flight) -> FlyingGroup[Any]:
    """The flight's group in the written mission, by its live group name."""
    [group] = [g for g in aircraft_groups(mission) if g.name == flight.live_group_name]
    return group


def parked(game: Game, squadron: Squadron) -> int:
    written = game.live.written_map
    assert written is not None
    return parked_per_squadron(game, written)[squadron]


def to_racetrack(flight: Flight) -> dict[str, dict[str, Any]]:
    """The flight's two aircraft halfway to the racetrack's start, apart."""
    points = lay_out(flight)
    x, y = along(points[0], points[1], 0.5)
    return {
        "LIVE 1-1": flying(x, y, alt=6000.0, hdg=10.0, spd=200.0, fuel=FUEL[0]),
        "LIVE 1-2": flying(
            x - 300.0, y + 200.0, alt=6100.0, hdg=350.0, spd=200.0, fuel=FUEL[1]
        ),
    }


def test_resumed_flight_written(own: Game, start_miz: Path, tmp_path: Path) -> None:
    squadron = red_squadron(own)
    flight = plan_by_hand(own, 2, squadron)
    launch(own, flight, "LIVE 1")
    air = to_racetrack(flight)
    tick_of(own, start_miz, launched=[flight], air=air)
    state = flight.state
    racetrack = flight.flight_plan.waypoints[1].position

    result, mission = restore(own, tmp_path)

    group = written_group(mission, flight)
    assert len(group.units) == 2
    assert not group.late_activation and not group.uncontrolled
    fuel_max = flight.unit_type.dcs_unit_type.fuel_max
    for unit, tick, fuel in zip(group.units, air.values(), FUEL):
        assert math.dist((unit.position.x, unit.position.y), (tick["x"], tick["y"])) < 1
        assert unit.alt == pytest.approx(tick["alt"], abs=1)
        assert unit.heading == pytest.approx(tick["hdg"], abs=1)
        assert unit.fuel == pytest.approx(fuel * fuel_max, abs=1)
    assert group.points[0].speed == pytest.approx(200.0)
    assert (
        math.dist(
            (group.points[1].position.x, group.points[1].position.y),
            (racetrack.x, racetrack.y),
        )
        < 1
    )
    assert flight.launched and flight.live_group_name == str(group.name)
    written = own.live.written_map
    assert written is not None
    assert written.flight_units(flight) == [str(u.name) for u in group.units]
    assert flight.count == 2
    assert flight.state is state
    assert (result.in_air, result.resumed) == (1, 2)
    assert parked(own, squadron) == squadron.owned_aircraft - 2


def task_ids(tasks: Any) -> Iterator[str]:
    """The ids of the tasks, and of the tasks and actions they wrap."""

    def walk(v: Any) -> Iterator[str]:
        if isinstance(v, dict):
            if isinstance(v.get("id"), str):
                yield v["id"]
            for x in v.values():
                yield from walk(x)

    for task in tasks:
        yield from walk(task.dict())


def test_resumed_orbit_task_on_point_zero(
    own: Game, start_miz: Path, tmp_path: Path
) -> None:
    flight = plan_by_hand(own, 2, red_squadron(own))
    launch(own, flight, "LIVE 1")
    points = lay_out(flight)
    x, y = along(points[1], points[2], 0.5)
    plan = flight.flight_plan
    begins = plan.tot_for_waypoint(plan.waypoints[1])
    ends = plan.depart_time_for_waypoint(plan.waypoints[2])
    assert begins is not None and ends is not None
    tick_of(
        own,
        start_miz,
        launched=[flight],
        air={"LIVE 1-1": flying(x, y, alt=7000.0), "LIVE 1-2": flying(x, y + 50)},
        at=begins + (ends - begins) / 2,
    )

    _, mission = restore(own, tmp_path)

    group = written_group(mission, flight)
    assert "Orbit" in set(task_ids(group.points[0].tasks))


def test_mixed_flight_resumes_the_airborne(
    own: Game, start_miz: Path, tmp_path: Path
) -> None:
    squadron = red_squadron(own)
    flight = plan_by_hand(own, 3, squadron)
    launch(own, flight, "LIVE 1")
    own.live.landed.add("LIVE 1-2")
    pilots = list(flight.roster.iter_pilots())
    air = to_racetrack(flight)
    tick_of(
        own,
        start_miz,
        launched=[flight],
        air={"LIVE 1-1": air["LIVE 1-1"], "LIVE 1-2": {"inAir": False}},
        gone={"LIVE 1-3"},
    )

    _, mission = restore(own, tmp_path)

    assert len(written_group(mission, flight).units) == 1
    assert flight.count == 1
    assert list(flight.roster.iter_pilots()) == [pilots[0]]
    held = sum(f.count for f in ato_flights(own) if f.squadron is squadron)
    assert squadron.untasked_aircraft == squadron.owned_aircraft - held
    for pilot in pilots[1:]:
        assert any(p is pilot for p in squadron.available_pilots)
    assert parked(own, squadron) == squadron.owned_aircraft - 1


def test_unit_without_fuel_gets_planned_fuel(
    own: Game, start_miz: Path, tmp_path: Path
) -> None:
    """Review Focus 4: no fuel in the tick, the planned fuel."""
    flight = plan_by_hand(own, 2, red_squadron(own))
    launch(own, flight, "LIVE 1")
    air = to_racetrack(flight)
    del air["LIVE 1-1"]["fuel"]
    tick_of(own, start_miz, launched=[flight], air=air)

    _, mission = restore(own, tmp_path)

    lead = written_group(mission, flight).units[0]
    assert lead.fuel == pytest.approx(flight.fuel, abs=1)


#: A real F-16's planned pylons and its full tick ammo
#: (`tests/companionlink/data/resume_ammo.json`).
F16 = "F-16CM Fighting Falcon (Block 50)"


def fixture_entry(aircraft: str) -> dict[str, Any]:
    path = Path(__file__).parent / "companionlink" / "data" / "resume_ammo.json"
    return next(
        e
        for e in json.loads(path.read_text())
        if e["aircraft"] == aircraft and e["full"]
    )


def fixture_loadout(entry: dict[str, Any]) -> Loadout:
    pylons: dict[int, Optional[Weapon]] = {
        int(n): Weapon.with_clsid(clsid) for n, clsid in entry["pylons"].items()
    }
    return Loadout("planned", pylons, None)


def test_fired_weapons_left_off(
    own: Game, start_miz: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    entry = fixture_entry(F16)
    squadron = next(s for s in squadrons(own) if s.aircraft.variant_id == F16)
    flight = plan_by_hand(own, 2, squadron)
    for member in flight.iter_members():
        member.loadout = fixture_loadout(entry)
    launch(own, flight, "LIVE 1")
    air = to_racetrack(flight)
    fired = "weapons.bombs.CBU_105"
    air["LIVE 1-1"]["ammo"] = [a for a in entry["ammo"] if a[0] != fired]
    air["LIVE 1-2"]["ammo"] = [*entry["ammo"], ["weapons.missiles.NOPE_9", 1]]
    for unit in air.values():
        del unit["fuel"]  # the tanks stay
    tick_of(own, start_miz, launched=[flight], air=air)

    result, mission = restore(own, tmp_path)

    lead, wing = written_group(mission, flight).units
    planned = {int(n): c for n, c in entry["pylons"].items()}
    cbu = {n for n, c in planned.items() if "CBU-105" in c}
    assert cbu, "the F-16 carries CBU-105s"

    def pylons(unit: Any) -> dict[int, str]:
        return {int(n): p["CLSID"] for n, p in unit.pylons.items()}

    assert pylons(lead) == {n: c for n, c in planned.items() if n not in cbu}
    assert pylons(wing) == planned
    assert result.planned_loadout == 1
    assert (
        f"Resume: {flight} {wing.name}: planned loadout "
        "(weapons.missiles.NOPE_9 is not on its pylons)"
    ) in [r.getMessage() for r in caplog.records]


def test_resumed_flight_from_a_damaged_runway(
    own: Game, start_miz: Path, tmp_path: Path
) -> None:
    """Review Focus 1: a flight in the air is written whatever its runway."""
    squadron = red_squadron(own)
    flight = plan_by_hand(own, 2, squadron)
    launch(own, flight, "LIVE 1")
    tick_of(own, start_miz, launched=[flight], air=to_racetrack(flight))
    assert isinstance(squadron.location, Airfield)
    squadron.location.damage_runway()
    assert not squadron.location.runway_is_operational()

    _, mission = restore(own, tmp_path)

    assert len(written_group(mission, flight).units) == 2
    assert flight.count == 2


#: A group id no group of the restored mission has.
STALE = 4242


def group_ids(mission: Mission) -> set[int]:
    ids: set[int] = set()
    for coalition in mission.coalition.values():
        for country in coalition.countries.values():
            groups: list[Any] = [
                *country.plane_group,
                *country.helicopter_group,
                *country.vehicle_group,
                *country.ship_group,
                *country.static_group,
            ]
            ids.update(group.id for group in groups)
    return ids


def referenced_ids(mission: Mission) -> set[int]:
    """The group ids the mission's aircraft tasks, triggers and Retribution's
    escort data name."""
    found: set[int] = set()

    def walk(v: Any, key: Any = None) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)
        elif isinstance(v, (list, tuple)):
            if key == "ai_task" and v:
                found.add(int(v[0]))
            for x in v:
                walk(x)
        elif key in ("groupId", "group") and isinstance(v, int):
            found.add(v)

    for group in aircraft_groups(mission):
        for point in group.points:
            for task in point.tasks:
                walk(task.dict())
        for task in group.tasks:
            walk(task.dict())
    for trigger in mission.triggerrules.triggers:
        walk(trigger.dict())
        for action in trigger.actions:
            if isinstance(action, DoScript):
                try:
                    text = str(action.text)
                except KeyError:  # a script written in place, not a string id
                    text = str(action.text.id)
                for m in re.finditer(r"escort(?:ed)?GroupId\W+(\d+)", text):
                    found.add(int(m.group(1)))
    return found


def test_escort_resumed_without_its_target(
    own: Game, start_miz: Path, tmp_path: Path
) -> None:
    """Review Focus 2: the escort's target flight, not in the restored
    mission, is named by no task or trigger, not even by a stale group id."""
    red = own.red
    strikers = next(
        s
        for s in red.air_wing.iter_squadrons()
        if s.aircraft.variant_id == "Su-24M Fencer-D"
    )
    escorts = next(
        s
        for s in red.air_wing.iter_squadrons()
        if s.aircraft.variant_id == "MiG-23MLD Flogger-K"
    )
    target = next(
        tgo
        for cp in own.theater.controlpoints
        if cp.captured.is_blue
        for tgo in cp.ground_objects
        if FlightType.STRIKE in tgo.mission_types(red.player)
    )
    package = Package(target, own.db.flights, auto_asap=True)
    strike = Flight(package, strikers, 2, FlightType.STRIKE, StartType.COLD, None)
    package.add_flight(strike)
    escort = Flight(package, escorts, 2, FlightType.ESCORT, StartType.COLD, None)
    package.add_flight(escort)
    for f in package.flights:
        f.recreate_flight_plan()
    package.set_tot_asap(own.conditions.start_time)
    red.ato.add_package(package)
    strike.group_id = STALE
    launch(own, escort, "LIVE 1")
    waypoints = escort.flight_plan.waypoints
    join = next(
        i for i, w in enumerate(waypoints) if w.waypoint_type is FlightWaypointType.JOIN
    )
    x, y = along(waypoints[join].position, waypoints[join + 1].position, 0.5)
    at = escort.flight_plan.depart_time_for_waypoint(waypoints[join])
    if at is None:
        at = escort.flight_plan.tot_for_waypoint(waypoints[join])
    assert at is not None
    tick_of(
        own,
        start_miz,
        launched=[escort],
        air={"LIVE 1-1": flying(x, y, alt=5000.0), "LIVE 1-2": flying(x, y + 50)},
        at=max(at, own.conditions.start_time) + timedelta(minutes=1),
    )

    _, mission = restore(own, tmp_path)

    assert len(written_group(mission, escort).units) == 2
    ids = group_ids(mission)
    assert STALE not in ids
    references = referenced_ids(mission)
    assert references, "the escort's task names a group"
    assert references <= ids
    assert strike.group_id == STALE, "put back after the generation"


def test_failed_resume_write_changes_nothing(
    own: Game, start_miz: Path, tmp_path: Path
) -> None:
    squadron = red_squadron(own)
    flight = plan_by_hand(own, 2, squadron)
    launch(own, flight, "LIVE 1")
    air = to_racetrack(flight)
    tick_of(
        own,
        start_miz,
        launched=[flight],
        air={"LIVE 1-1": air["LIVE 1-1"], "LIVE 1-2": {"inAir": False}},
    )
    members = list(flight.iter_members())
    state = flight.state
    untasked = {s: s.untasked_aircraft for s in squadrons(own)}
    pilots = {s: list(s.available_pilots) for s in squadrons(own)}
    blocker = tmp_path / "file"
    blocker.write_text("")

    with pytest.raises(OSError):
        write_restore(own, blocker / "retribution_dynamic.miz")

    assert list(flight.iter_members()) == members
    assert flight.count == 2
    assert flight.state is state
    assert flight.launched and flight.live_group_name == "LIVE 1"
    assert {s: s.untasked_aircraft for s in squadrons(own)} == untasked
    assert {s: list(s.available_pilots) for s in squadrons(own)} == pilots
