"""A package's Start time, derived from its Time Over Target.

The package keeps one stored time, its TOT. Its Start time is when its first
flight starts up: the earliest `startup_time()` of its flights. Moving one moves
the other by the same amount. The editors of a dynamic campaign go through
here; the campaign's current time is the floor of an edit.
"""

from __future__ import annotations

import logging
import weakref
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .flight import Flight
    from .package import Package
    from .starttype import StartType

# Flights whose start-up time could not be worked out, logged once each.
_untimed: weakref.WeakSet[Flight] = weakref.WeakSet()


def package_start_time(package: Package) -> datetime | None:
    """The earliest start-up time of the package's flights.

    A flight whose times cannot be worked out is left out; None when no flight
    is left.
    """
    starts = [
        start
        for start in (_startup(flight) for flight in package.flights)
        if start is not None
    ]
    return min(starts, default=None)


def move_start(package: Package, start: datetime, now: datetime) -> None:
    """Moves the TOT so the package starts at `start`, or now if that is earlier."""
    current = package_start_time(package)
    if current is None:
        return
    package.time_over_target += max(start, now) - current


def move_tot(package: Package, tot: datetime, now: datetime) -> None:
    """Sets the TOT, moved later if the package would start before now."""
    package.time_over_target = tot
    start = package_start_time(package)
    if start is not None and start < now:
        package.time_over_target += now - start


def change_start_type(
    flight: Flight, start_type: StartType, now: datetime, locked: bool = False
) -> None:
    """Sets the flight's start type, keeping its package's Start time.

    The TOT moves by the change in the package's Start time. With ASAP the TOT
    is worked out from now instead. A `locked` package (a flight of it in the
    live mission or on its way there) keeps its TOT: only this flight's times
    move.
    """
    package = flight.package
    before = package_start_time(package)
    flight.start_type = start_type
    if locked:
        return
    if package.auto_asap:
        package.set_tot_asap(now)
        return
    keep_start(package, before)


def keep_start(package: Package, start: datetime | None) -> None:
    """Moves the TOT so the package starts at `start` again, after its flights
    changed (a start type, a flight added).

    Nothing with ASAP (the TOT is worked out from now) or when the package had
    no Start time before the change (its first flight).
    """
    if start is None or package.auto_asap:
        return
    after = package_start_time(package)
    if after is not None:
        package.time_over_target += start - after


def start_asap(package: Package, now: datetime) -> None:
    """Fixes the TOT so the package's first flight starts now; clears ASAP."""
    package.set_tot_asap(now)
    package.auto_asap = False


def _startup(flight: Flight) -> datetime | None:
    try:
        return flight.flight_plan.startup_time()
    except Exception:
        if flight not in _untimed:
            _untimed.add(flight)
            logging.exception(f"No start-up time for {flight}; left out of its package")
        return None
