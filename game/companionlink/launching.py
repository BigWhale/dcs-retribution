"""When a flight of the ATO is due in the live mission.

A dynamic campaign launches each flight of the blue and red ATO into the
running mission at its flight plan's Start time, when it enters the mission
and starts up (`FlightPlan.startup_time()`). The launcher asks every second; this module
answers which flights are due, from the ATO as it stands at that moment.
"""

from __future__ import annotations

import logging
import weakref
from datetime import datetime
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from game import Game
    from game.ato.flight import Flight

#: Flights whose start time could not be computed, already logged once.
_untimed: weakref.WeakSet[Flight] = weakref.WeakSet()


def due_flights(
    game: Game,
    now: datetime | None,
    receiving: bool,
    in_progress: set[Flight],
    failed: set[Flight],
) -> list[Flight]:
    """The flights to launch at `now`, in start-time order.

    None unless the campaign is dynamic, the link is receiving, the running
    mission is the campaign's own (`game.live.following_own_mission`) and the
    mission's time is known. Otherwise every flight of both ATOs that is not
    launched, not being launched (`in_progress`) and not failed, whose start
    time is `now` or earlier. A flight whose start time cannot be computed is
    skipped.
    """
    if not game.dynamic_campaign or not receiving or now is None:
        return []
    if not game.live.following_own_mission:
        return []
    due: list[tuple[datetime, Flight]] = []
    for flight in _flights(game):
        if flight.launched or flight in in_progress or flight in failed:
            continue
        start = _start(flight)
        if start is not None and start <= now:
            due.append((start, flight))
    due.sort(key=lambda entry: entry[0])
    return [flight for _, flight in due]


def _flights(game: Game) -> Iterator[Flight]:
    for coalition in (game.blue, game.red):
        for package in coalition.ato.packages:
            yield from package.flights


def _start(flight: Flight) -> datetime | None:
    try:
        return flight.flight_plan.startup_time()
    except Exception:
        if flight not in _untimed:
            _untimed.add(flight)
            logging.exception(f"No start time for {flight}; it is not launched")
        return None
