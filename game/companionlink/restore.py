"""A dynamic campaign's restored mission: written again from the tick.

The live record keeps the newest snapshot of the campaign's own mission (the
tick, `game.live.tick`). A restore writes the campaign's mission again from it,
starting at the tick's minute (DCS starts a mission at any minute), with a new
marker:

1. The tick is parsed; the start is its absolute time, seconds dropped.
2. The launched flights are sorted by their names in the name map, the game
   unchanged: a unit in the air, the flight is *in the air* and stays
   launched; else a name seen landing, it is *finished*; else (on the ground,
   never in the air, not in the tick at all) it is *launched again* at the
   restored mission's first tick. Of a flight in the air, the aircraft the
   tick has flying (`game.companionlink.resume`) resume; one with none (only
   a player's, or the dead's) or no route leg to fly on is launched again.
3. The ground war is planned again from the bases' armor, as a turn does.
4. The mission is written under a new marker, with the flights in the air
   only: each resumes with its flying aircraft, its roster shrunk to them and
   its state `Resumed` for the generation; no other flight is in the file.
   Each squadron parks its aircraft but the resumed ones. Ground units,
   ships, convoys and cargo ships stand where the tick saw them, by their
   names in the name map; a convoy or cargo ship rejoins its route at the
   point nearest to it. A unit the game has alive that the tick does not
   stays where it is generated; the dead are not generated. It is written
   beside the path and moved onto it once whole. The flights' states and
   group ids are put back afterwards, written or failed.
5. Only once the file is written: the resumed flights keep their shrunk
   rosters and fly as the generated groups (`live_group_name`), the finished
   flights leave the ATO, the launched-again ones are not launched any more,
   the squadrons hold what the ATO holds, the new name map is kept, the
   campaign's time is the start, and the campaign's earlier missions are
   retired, so the mission replaced, if it still runs, is another mission:
   not followed, nothing launched into it, its entries dropped.
6. A failure before 5 puts the marker, the written name map, the planning
   latch, the ground war and the rosters back and raises; the campaign is as
   before.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence, TYPE_CHECKING
from uuid import UUID

from dcs import Mission
from dcs.mapping import Point
from dcs.unitgroup import FlyingGroup, Group, StaticGroup, VehicleGroup

from game.companionlink.livelosses import finish_flight
from game.companionlink.liverecord import LiveTick
from game.companionlink.liveunitmap import LiveUnitMap
from game.companionlink.resume import ResumePlan, plan_resume
from game.companionlink.snapshot import LiveSnapshot
from game.ground_forces.ai_ground_planner import GroundPlanner
from game.missiongenerator import MissionGenerator

if TYPE_CHECKING:
    from game import Game
    from game.ato.flight import Flight
    from game.ato.flightmember import FlightMember
    from game.ato.flightstate import FlightState
    from game.dcs.groundunittype import GroundUnitType
    from game.squadrons import Squadron
    from game.theater import ControlPoint
    from game.unitmap import UnitMap

log = logging.getLogger("game.companionlink")

#: Where the tick saw a unit: x north, y east (map metres), heading degrees.
Spot = tuple[float, float, float]


class RestoreError(ValueError):
    """The game cannot be restored: not a dynamic campaign, or no tick."""


def restore_start(tick: LiveTick) -> datetime:
    """The restored mission's start: the tick's time, seconds dropped."""
    return tick.abs_time.replace(second=0, microsecond=0)


@dataclass
class LaunchedAtTick:
    """The launched flights of both ATOs, sorted by the tick."""

    in_air: list[Flight]
    again: list[Flight]
    finished: list[Flight]


def sort_launched(game: Game, tick: LiveSnapshot) -> LaunchedAtTick:
    """Sorts the launched flights by their names in the name map; changes
    nothing."""
    record = game.live
    flying = {u.name for u in tick.units if u.in_air}
    sorted_ = LaunchedAtTick(in_air=[], again=[], finished=[])
    for coalition in (game.blue, game.red):
        for package in coalition.ato.packages:
            for flight in package.flights:
                if not flight.launched:
                    continue
                names = record.name_map.flight_units(flight)
                if any(n in flying for n in names):
                    sorted_.in_air.append(flight)
                elif any(n in record.landed for n in names):
                    sorted_.finished.append(flight)
                else:
                    sorted_.again.append(flight)
    return sorted_


def plan_resumes(
    game: Game, launched: LaunchedAtTick, tick: LiveSnapshot, at: datetime
) -> tuple[LaunchedAtTick, list[ResumePlan]]:
    """Each flight in the air's resume, taken at `at`; a flight with nothing
    to resume, or no route leg to fly on, is moved to the launched again.
    Changes nothing."""
    in_air: list[Flight] = []
    again: list[Flight] = []
    plans: list[ResumePlan] = []
    for flight in launched.in_air:
        try:
            plan = plan_resume(game, flight, tick, at)
        except ValueError as e:  # a flight plan with no route leg
            log.warning("Resume: %s cannot resume (%s): launched again", flight, e)
            plan = None
        if plan is None:
            again.append(flight)
            continue
        in_air.append(flight)
        plans.append(plan)
    sorted_ = LaunchedAtTick(
        in_air=in_air, again=launched.again + again, finished=launched.finished
    )
    return sorted_, plans


class RestorePlacement:
    """Where the restored mission's units stand, from the tick, and how many
    aircraft each squadron parks.

    Each tick unit the name map knows gives its counterpart's spot: a theater
    unit (TGO unit, static or ship) and a cargo ship their own; a front-line
    unit the next of its unit type's and base's, an old group at a time; a
    convoy unit the next of its convoy's and unit type's. A tick unit the map
    does not know is ignored.
    """

    def __init__(
        self,
        game: Game,
        tick: LiveSnapshot,
        launched: LaunchedAtTick,
        plans: Sequence[ResumePlan],
    ) -> None:
        self.game = game
        self.tick = tick
        self.launched = launched
        #: The resumes of the flights in the air.
        self.plans = tuple(plans)
        #: Units placed where the tick has them.
        self.placed = 0
        #: Units the game has alive that the tick does not: left where
        #: generated.
        self.unplaced = 0
        name_map = game.live.name_map
        # Keyed by identity: the name map and the game hold the same objects.
        self._theater: dict[int, Spot] = {}
        self._cargo: dict[int, Spot] = {}
        self._convoy: dict[tuple[int, str], deque[Spot]] = {}
        front: dict[tuple[str, UUID], dict[str, list[Spot]]] = {}
        for u in tick.units:
            spot = (u.x, u.y, u.heading)
            theater = name_map.theater_objects.get(u.name)
            front_unit = name_map.front_line_units.get(u.name)
            convoy = name_map.convoys.get(u.name)
            cargo = name_map.cargo_ships.get(u.name)
            if theater is not None:
                self._theater[id(theater.theater_unit)] = spot
            elif front_unit is not None:
                key = (front_unit.unit_type.variant_id, front_unit.origin.id)
                front.setdefault(key, {}).setdefault(u.group, []).append(spot)
            elif convoy is not None:
                ckey = (id(convoy.convoy), convoy.unit_type.variant_id)
                self._convoy.setdefault(ckey, deque()).append(spot)
            elif cargo is not None:
                self._cargo[id(cargo)] = spot
        self._front: dict[tuple[str, UUID], deque[Spot]] = {
            key: deque(s for spots in groups.values() for s in spots)
            for key, groups in front.items()
        }

    def parked(self, squadron: Squadron) -> int:
        """The squadron's aircraft but the resumed ones."""
        flying = sum(
            len(p.members) for p in self.plans if p.state.flight.squadron is squadron
        )
        return max(0, squadron.owned_aircraft - flying)

    def place_ground(self, mission: Mission, unit_map: UnitMap) -> None:
        """Places the generated theater units, ships, convoys and cargo ships
        where the tick saw them; a convoy or cargo ship then rejoins its
        route."""
        for group in _ground_groups(mission):
            names = [str(u.name) for u in group.units]
            if names[0] in unit_map.convoys:
                spots = [self._next_convoy_spot(unit_map, n) for n in names]
                self._place(group, spots)
                _rejoin_route(group)
            elif names[0] in unit_map.cargo_ships:
                ship = unit_map.cargo_ships[names[0]]
                self._place(group, [self._cargo.get(id(ship))])
                _rejoin_route(group)
            elif all(n in unit_map.theater_objects for n in names):
                units = [unit_map.theater_objects[n].theater_unit for n in names]
                if not all(u.alive for u in units):
                    continue  # a dead static, generated dead where it stood
                self._place(group, [self._theater.get(id(u)) for u in units])

    def place_front_line(
        self, group: VehicleGroup, origin: ControlPoint, unit_type: GroundUnitType
    ) -> None:
        """Places a front-line group's units on the next spots the tick has
        for its unit type and base."""
        spots = self._front.get((unit_type.variant_id, origin.id), deque())
        self._place(group, [spots.popleft() if spots else None for _ in group.units])

    def _next_convoy_spot(self, unit_map: UnitMap, name: str) -> Optional[Spot]:
        unit = unit_map.convoys[name]
        spots = self._convoy.get((id(unit.convoy), unit.unit_type.variant_id))
        return spots.popleft() if spots else None

    def _place(self, group: Group[Any, Any], spots: Sequence[Optional[Spot]]) -> None:
        """Each unit on its spot, one with none left where generated; then
        the group's point 0 at its first unit."""
        terrain = self.game.theater.terrain
        for unit, spot in zip(group.units, spots):
            if spot is None:
                self.unplaced += 1
                continue
            x, y, heading = spot
            # A new point: a generated unit may share its position with the
            # game's theater unit.
            unit.position = Point(x, y, terrain)
            unit.heading = heading
            self.placed += 1
        first = group.units[0]
        if isinstance(group, StaticGroup):
            group.heading = first.heading
        if group.points:
            group.points[0].position = Point(
                first.position.x, first.position.y, terrain
            )


@dataclass(frozen=True)
class RestoreResult:
    path: Path
    start: datetime
    minute: int
    placed: int
    unplaced: int
    in_air: int
    again: int
    finished: int
    #: The aircraft of the flights in the air written into the mission.
    resumed: int
    #: Those of them given their planned loadout (`Resume: …` logged).
    planned_loadout: int


def write_restore(game: Game, path: Path) -> RestoreResult:
    """Writes the campaign's mission again from the tick to `path`; the game
    changes only once the file is written."""
    if not game.dynamic_campaign:
        raise RestoreError("Not a dynamic campaign")
    tick = game.live.tick
    if tick is None:
        raise RestoreError("No snapshot of the campaign's mission is kept")
    snapshot = tick.snapshot()
    start = restore_start(tick)
    launched, plans = plan_resumes(
        game, sort_launched(game, snapshot), snapshot, tick.abs_time
    )
    placement = RestorePlacement(game, snapshot, launched, plans)

    record = game.live
    marker, written_map = record.marker, record.written_map
    planning_open = record.planning_open
    planners = game.ground_planners
    flights = [
        f
        for coalition in (game.blue, game.red)
        for package in coalition.ato.packages
        for f in package.flights
    ]
    # Put back after the generation, written or failed.
    states: list[tuple[Flight, FlightState, int]] = [
        (f, f.state, f.group_id) for f in flights
    ]
    # Put back on a failure only.
    rosters: list[tuple[Flight, list[FlightMember]]] = [
        (p.state.flight, p.state.flight.roster.members) for p in plans
    ]
    group_names: dict[int, str] = {}
    # Written beside `path` and moved onto it whole: a failed write leaves the
    # mission written before as it was.
    temporary = path.with_name(path.name + ".tmp")
    try:
        try:
            game.ground_planners = _plan_ground_war(game)
            record.new_marker()
            # No flight carries a group id from an earlier generation: a task
            # or trigger naming one would name a group the file may not have.
            for f in flights:
                f.group_id = 0
            for plan in plans:
                plan.state.flight.roster.members = list(plan.members)
                plan.state.flight.set_state(plan.state)
            generator = MissionGenerator(game, start, restore=placement)
            unit_map = generator.generate_miz(temporary)
            for plan in plans:
                flight = plan.state.flight
                group = generator.mission.find_group_by_id(flight.group_id)
                if flight.group_id <= 0 or not isinstance(group, FlyingGroup):
                    raise RuntimeError(f"{flight} was not generated")
                group_names[id(flight)] = str(group.name)
            name_map = LiveUnitMap.from_generation(unit_map, game)
            os.replace(temporary, path)
        finally:
            for f, state, group_id in states:
                f.set_state(state)
                f.group_id = group_id
    except BaseException:
        record.marker, record.written_map = marker, written_map
        record.planning_open = planning_open
        game.ground_planners = planners
        for f, members in rosters:
            f.roster.members = members
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise

    for (flight, members), plan in zip(rosters, plans):
        flight.live_group_name = group_names[id(flight)]
        kept = {id(m) for m in plan.members}
        for member in members:
            if id(member) not in kept and member.tgp_laser_code is not None:
                member.release_tgp_laser_code()
    for flight in launched.finished:
        finish_flight(flight)
    for flight in launched.again:
        flight.launched = False
        flight.live_group_name = None
    for coalition in (game.blue, game.red):
        held = [f for package in coalition.ato.packages for f in package.flights]
        for squadron in coalition.air_wing.iter_squadrons():
            squadron.return_all_but_held_by(held)
    record.written(name_map)
    game.conditions.start_time = start
    record.retire()

    result = RestoreResult(
        path=path,
        start=start,
        minute=tick.minute,
        placed=placement.placed,
        unplaced=placement.unplaced,
        in_air=len(launched.in_air),
        again=len(launched.again),
        finished=len(launched.finished),
        resumed=sum(len(p.members) for p in plans),
        planned_loadout=sum(len(p.state.planned_loadout) for p in plans),
    )
    log.info(
        "Restore: %s written, starting %s, from minute %d; %d units placed, "
        "%d where generated; flights: %d in the air (%d aircraft resumed, "
        "%d on planned loadout), %d to launch again, %d finished",
        result.path,
        f"{result.start:%Y-%m-%d %H:%M}",
        result.minute,
        result.placed,
        result.unplaced,
        result.in_air,
        result.resumed,
        result.planned_loadout,
        result.again,
        result.finished,
    )
    return result


def _ground_groups(mission: Mission) -> list[Group[Any, Any]]:
    """The mission's vehicle, ship and static groups."""
    groups: list[Group[Any, Any]] = []
    for coalition in mission.coalition.values():
        for country in coalition.countries.values():
            groups.extend(country.vehicle_group)
            groups.extend(country.ship_group)
            groups.extend(country.static_group)
    return groups


def _rejoin_route(group: Group[Any, Any]) -> None:
    """Drops the route points before the one nearest the group's first unit,
    so it goes on from there; point 0 stays."""
    if len(group.points) < 3:
        return
    lead = group.units[0].position
    later = group.points[1:]
    nearest = min(
        range(len(later)), key=lambda i: lead.distance_to_point(later[i].position)
    )
    group.points[1:] = later[nearest:]


def _plan_ground_war(game: Game) -> dict[UUID, GroundPlanner]:
    """Each front's ground war planned from today's armor, as a turn's
    initialization plans it."""
    planners: dict[UUID, GroundPlanner] = {}
    for cp in game.theater.controlpoints:
        if cp.has_frontline:
            planner = GroundPlanner(cp, game)
            planner.plan_groundwar()
            planners[cp.id] = planner
    return planners
