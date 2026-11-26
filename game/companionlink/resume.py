"""A dynamic campaign's flights in the air at the tick, resumed in the
restored mission where the tick saw them.

Of a launched flight in the air at the tick, an aircraft resumes when the tick
has it in the air, flown by the AI, and it is not counted dead; the rest go
back to their squadron. The first to resume is the lead. A tick unit's name is
paired with its roster member by the pilot the name map records for it, so a
restore retried before the restored mission is followed (the roster already
shrunk, the map and tick still the old mission's) resumes the same aircraft.

The flight flies on to the waypoint that ends the route leg nearest the lead.
The legs run from the takeoff waypoint to the landing (the last waypoint when
there is none). Legs within `TIE_M` of the nearest are tied: of those, the one
whose planned time span is nearest the tick's time wins. A flight past every
planned time (late) is placed by its position alone, so a flight behind its
schedule is never moved ahead of where it is.

The `Resumed` state exists only while the restore generates the mission.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence, TYPE_CHECKING

from dcs import Point

from game.ato.flightplans.planningerror import PlanningError
from game.ato.flightstate import Navigating
from game.ato.flightwaypointtype import FlightWaypointType
from game.companionlink.snapshot import LiveSnapshot, LiveUnit
from game.utils import Distance, Speed, meters, mps

if TYPE_CHECKING:
    from game import Game
    from game.ato.flight import Flight
    from game.ato.flightmember import FlightMember
    from game.ato.flightplans.flightplan import FlightPlan
    from game.ato.flightwaypoint import FlightWaypoint
    from game.settings import Settings

#: Route legs within this many metres of the nearest count as tied.
TIE_M = 5_000.0

#: A leg's planned time span: when the flight leaves its first waypoint and
#: reaches its second.
Span = tuple[datetime, datetime]


@dataclass(frozen=True)
class ResumedUnit:
    """A resumed aircraft as the tick saw it."""

    name: str
    x: float  # map metres, north
    y: float  # map metres, east
    alt: float  # metres above sea level
    heading: float  # degrees
    speed: float  # m/s
    fuel: Optional[float]  # a fraction of internal fuel
    ammo: Optional[tuple[tuple[str, int], ...]]

    @classmethod
    def from_live(cls, unit: LiveUnit) -> ResumedUnit:
        return cls(
            name=unit.name,
            x=unit.x,
            y=unit.y,
            alt=unit.alt,
            heading=unit.heading,
            speed=unit.speed,
            fuel=unit.fuel,
            ammo=unit.ammo,
        )


class Resumed(Navigating):
    """A flight in the air at the tick, flying from its lead's tick position
    to the waypoint after `waypoint_index`."""

    def __init__(
        self,
        flight: Flight,
        settings: Settings,
        waypoint_index: int,
        units: Sequence[ResumedUnit],
    ) -> None:
        super().__init__(flight, settings, waypoint_index)
        #: The resumed aircraft, in roster order; the first is the lead.
        self.units = tuple(units)
        #: The units given their planned loadout (set by the configurator).
        self.planned_loadout: list[str] = []

    @property
    def lead(self) -> ResumedUnit:
        return self.units[0]

    def estimate_position(self) -> Point:
        return self.current_waypoint.position.new_in_same_map(self.lead.x, self.lead.y)

    def estimate_altitude(self) -> tuple[Distance, str]:
        return meters(self.lead.alt), "BARO"

    def estimate_speed(self) -> Speed:
        return mps(self.lead.speed)

    def estimate_fuel(self) -> float:
        """The lead's fuel in kg; the planned estimate when the tick has
        none."""
        if self.lead.fuel is None:
            return super().estimate_fuel()
        return self.lead.fuel * self.flight.unit_type.dcs_unit_type.fuel_max


@dataclass
class ResumePlan:
    """The roster members that resume, and the flight's resume state."""

    members: list[FlightMember]
    state: Resumed


def plan_resume(
    game: Game, flight: Flight, tick: LiveSnapshot, at: datetime
) -> Optional[ResumePlan]:
    """The flight's aircraft that resume from the tick, taken at `at`; None
    when none does."""
    record = game.live
    seen = {u.name: u for u in tick.units}
    members: list[FlightMember] = []
    units: list[ResumedUnit] = []
    for name, member in _named_members(game, flight):
        unit = seen.get(name)
        if unit is None or not unit.in_air or unit.player is not None:
            continue
        if name in record.dead:
            continue
        members.append(member)
        units.append(ResumedUnit.from_live(unit))
    if not units:
        return None
    lead = units[0]
    index = flown_to_index(flight, lead.x, lead.y, at)
    state = Resumed(flight, game.settings, index - 1, units)
    return ResumePlan(members=members, state=state)


def _named_members(game: Game, flight: Flight) -> list[tuple[str, FlightMember]]:
    """The flight's unit names in the name map, each with its roster member,
    in the map's order.

    A name is paired with the member flown by the pilot the map records for
    it; a name whose member is no longer in the roster (a restore written
    before shrank it, and its mission is not followed yet) has none. Names
    mapped without a pilot are paired with the members without one, in
    order.
    """
    aircraft = game.live.name_map.aircraft
    no_pilot = iter([m for m in flight.iter_members() if m.pilot is None])
    paired: list[tuple[str, FlightMember]] = []
    for name in game.live.name_map.flight_units(flight):
        pilot = aircraft[name].pilot
        if pilot is None:
            member = next(no_pilot, None)
        else:
            member = next((m for m in flight.iter_members() if m.pilot is pilot), None)
        if member is not None:
            paired.append((name, member))
    return paired


def flown_to_index(flight: Flight, x: float, y: float, at: datetime) -> int:
    """The index in the flight plan's waypoints of the waypoint a flight at
    (x, y) (map metres) at `at` flies to; at least 1."""
    plan = flight.flight_plan
    waypoints = plan.waypoints
    last = next(
        (
            i
            for i, w in enumerate(waypoints)
            if w.waypoint_type is FlightWaypointType.LANDING_POINT
        ),
        len(waypoints) - 1,
    )
    if last < 1:
        raise ValueError(f"{flight} has no route leg")
    route = waypoints[: last + 1]
    times = _planned_times(plan, route)
    distances = {
        i: _to_segment(x, y, route[i - 1].position, route[i].position)
        for i in range(1, last + 1)
    }
    spans: dict[int, Optional[Span]] = {}
    for i in distances:
        leaves, reaches = times[i - 1][1], times[i][0]
        spans[i] = None if leaves is None or reaches is None else (leaves, reaches)
    nearest = min(distances, key=lambda i: distances[i])
    known = [span for span in spans.values() if span is not None]
    if known and all(at > max(span) for span in known):
        return nearest  # late: where it is decides
    tied = [i for i in distances if distances[i] <= distances[nearest] + TIE_M]

    def by_time(i: int) -> tuple[bool, timedelta, float]:
        span = spans[i]
        if span is None:
            return True, timedelta(), distances[i]
        return False, _gap(at, span), distances[i]

    return min(tied, key=by_time)


def _planned_times(
    plan: FlightPlan[Any], route: Sequence[FlightWaypoint]
) -> list[tuple[Optional[datetime], Optional[datetime]]]:
    """When the flight plans to reach and to leave each waypoint of `route`.

    A waypoint's TOT or departure time where the plan sets one; else the
    takeoff time for the first, and the time it left the waypoint before plus
    the travel time from it. None when a time cannot be computed.
    """
    times: list[tuple[Optional[datetime], Optional[datetime]]] = []
    for i, waypoint in enumerate(route):
        try:
            tot = plan.tot_for_waypoint(waypoint)
            departs = plan.depart_time_for_waypoint(waypoint)
            reaches = tot if tot is not None else departs
            if reaches is None:
                if i == 0:
                    reaches = plan.takeoff_time()
                else:
                    left = times[-1][1]
                    if left is not None:
                        travel = plan.travel_time_between_waypoints(
                            route[i - 1], waypoint
                        )
                        reaches = left + travel
            leaves = departs if departs is not None else reaches
        except (ArithmeticError, NotImplementedError, PlanningError):
            reaches = leaves = None
        times.append((reaches, leaves))
    return times


def _gap(at: datetime, span: Span) -> timedelta:
    """How far `at` is from the span; zero inside it."""
    begins, ends = min(span), max(span)
    if at < begins:
        return begins - at
    if at > ends:
        return at - ends
    return timedelta()


def _to_segment(x: float, y: float, a: Point, b: Point) -> float:
    """The distance from (x, y) to the segment a-b, in map metres."""
    dx, dy = b.x - a.x, b.y - a.y
    length2 = dx * dx + dy * dy
    t = 0.0
    if length2 > 0:
        t = max(0.0, min(1.0, ((x - a.x) * dx + (y - a.y) * dy) / length2))
    return math.hypot(x - (a.x + t * dx), y - (a.y + t * dy))
