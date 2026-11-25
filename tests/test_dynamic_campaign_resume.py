"""A dynamic campaign's resumed flights: which aircraft of a flight in the air
at the tick resume, and the waypoint they fly on to.

A launched flight's aircraft resume when the tick has them in the air, flown
by the AI and not counted dead; the lead is the first of them. The waypoint
flown to ends the route leg nearest the lead; legs within `TIE_M` of the
nearest are told apart by their planned times.

The flights are hand-planned BARCAPs: takeoff (0), the racetrack's start (1)
and end (2), the landing (3). The tests that need a route of a known shape
lay it out from the takeoff waypoint, so they do not depend on the theater.
"""

from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path
from typing import Any

from dcs.mapping import Point

from game.ato.flight import Flight
from game.ato.flightwaypointtype import FlightWaypointType
from game.companionlink.resume import (
    TIE_M,
    Resumed,
    ResumedUnit,
    flown_to_index,
    plan_resume,
)
from game.game import Game
from game.utils import meters
from tests.conftest import plan_by_hand
from tests.test_dynamic_campaign_restore import (  # noqa: F401
    begun,
    launch,
    own,
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
