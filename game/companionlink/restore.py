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
   restored mission's first tick.
3. The ground war is planned again from the bases' armor, as a turn does.
4. The mission is written under a new marker, with no flights: each squadron
   parks its aircraft but the living ones of its flights in the air.
5. Only once the file is written: the finished flights leave the ATO, the
   launched-again ones are not launched any more, the squadrons hold what the
   ATO holds, the new name map is kept, the campaign's time is the start, and
   the campaign's earlier missions are retired, so the mission replaced, if
   it still runs, is another mission: not followed, nothing launched into it,
   its entries dropped.
6. A failure before 5 puts the marker, the written name map and the ground
   war back and raises; the campaign is as before.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from game.companionlink.livelosses import finish_flight
from game.companionlink.liverecord import LiveTick
from game.companionlink.liveunitmap import LiveUnitMap
from game.companionlink.snapshot import LiveSnapshot
from game.ground_forces.ai_ground_planner import GroundPlanner
from game.missiongenerator import MissionGenerator

if TYPE_CHECKING:
    from game import Game
    from game.ato.flight import Flight
    from game.squadrons import Squadron

log = logging.getLogger("game.companionlink")


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


class RestorePlacement:
    """Where the restored mission's units stand, from the tick, and how many
    aircraft each squadron parks."""

    def __init__(
        self, game: Game, tick: LiveSnapshot, launched: LaunchedAtTick
    ) -> None:
        self.game = game
        self.tick = tick
        self.launched = launched
        #: Units placed where the tick has them.
        self.placed = 0
        #: Units the game has alive that the tick does not: left where
        #: generated.
        self.unplaced = 0

    def parked(self, squadron: Squadron) -> int:
        """The squadron's aircraft but the living ones of its flights in the
        air."""
        record = self.game.live
        flying = 0
        for flight in self.launched.in_air:
            if flight.squadron is not squadron:
                continue
            dead = sum(
                1 for n in record.name_map.flight_units(flight) if n in record.dead
            )
            flying += max(0, flight.count - dead)
        return max(0, squadron.owned_aircraft - flying)


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
    launched = sort_launched(game, snapshot)
    placement = RestorePlacement(game, snapshot, launched)

    record = game.live
    marker, written_map = record.marker, record.written_map
    planners = game.ground_planners
    try:
        game.ground_planners = _plan_ground_war(game)
        record.new_marker()
        unit_map = MissionGenerator(game, start, restore=placement).generate_miz(path)
        name_map = LiveUnitMap.from_generation(unit_map, game)
    except BaseException:
        record.marker, record.written_map = marker, written_map
        game.ground_planners = planners
        raise

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
    )
    log.info(
        "Restore: %s written, starting %s, from minute %d; %d units placed, "
        "%d where generated; flights: %d in the air, %d to launch again, "
        "%d finished",
        result.path,
        f"{result.start:%Y-%m-%d %H:%M}",
        result.minute,
        result.placed,
        result.unplaced,
        result.in_air,
        result.again,
        result.finished,
    )
    return result


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
