"""A package's Start time, and moving it with its Time Over Target.

The packages and flights are stand-ins. A flight's `startup_time()` is its
package's TOT minus a lead for its start type, and its
`minimum_duration_from_start_to_tot()` is the same lead, so a package's ASAP
goes through the real `TotEstimator`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from game.ato.packageschedule import (
    change_start_type,
    keep_start,
    move_start,
    move_tot,
    package_start_time,
    start_asap,
)
from game.ato.starttype import StartType
from game.ato.traveltime import TotEstimator

LEAD = {
    StartType.COLD: timedelta(minutes=40),
    StartType.WARM: timedelta(minutes=32),
    StartType.RUNWAY: timedelta(minutes=24, seconds=30),
    StartType.IN_FLIGHT: timedelta(minutes=24),
}


def at(hhmm: str, day: int = 13) -> datetime:
    hours, minutes = hhmm.split(":")
    return datetime(2019, 7, day, int(hours), int(minutes))


class FakePackage:
    def __init__(self, tot: datetime, auto_asap: bool = False) -> None:
        self.time_over_target = tot
        self.auto_asap = auto_asap
        self.flights: list[Any] = []

    def set_tot_asap(self, now: datetime) -> None:
        self.time_over_target = TotEstimator(self).earliest_tot(now)  # type: ignore[arg-type]


class FakeFlight:
    def __init__(self, package: FakePackage, start_type: StartType) -> None:
        self.package = package
        self.start_type = start_type
        package.flights.append(self)
        self.flight_plan = SimpleNamespace(
            startup_time=lambda: package.time_over_target - LEAD[self.start_type],
            minimum_duration_from_start_to_tot=lambda: LEAD[self.start_type],
        )


class UntimedFlight(FakeFlight):
    """A flight whose times cannot be worked out."""

    def __init__(self, package: FakePackage) -> None:
        super().__init__(package, StartType.COLD)

        def fail() -> datetime:
            raise RuntimeError("no startup time")

        self.flight_plan = SimpleNamespace(startup_time=fail)


def package(tot: datetime, *start_types: StartType, **kwargs: Any) -> Any:
    p = FakePackage(tot, **kwargs)
    for start_type in start_types:
        FakeFlight(p, start_type)
    return p


def test_start_time_is_earliest_startup() -> None:
    p = package(at("15:00"), StartType.IN_FLIGHT, StartType.COLD)

    assert package_start_time(p) == at("14:20")


def test_untimed_flight_left_out() -> None:
    p = package(at("15:00"))
    UntimedFlight(p)
    FakeFlight(p, StartType.WARM)

    assert package_start_time(p) == at("14:28")

    untimed = package(at("15:00"))
    UntimedFlight(untimed)
    UntimedFlight(untimed)

    assert package_start_time(untimed) is None
    move_start(untimed, at("16:00"), now=at("14:00"))
    assert untimed.time_over_target == at("15:00")


def test_start_and_tot_move_together_across_midnight() -> None:
    now = at("23:58")
    p = package(at("15:00"), StartType.COLD)

    move_start(p, at("00:10", day=14), now)

    assert p.time_over_target == at("00:50", day=14)
    assert package_start_time(p) == at("00:10", day=14)

    move_tot(p, at("23:59") + timedelta(minutes=40), now)

    assert package_start_time(p) == at("23:59")


def test_edit_before_now_is_clamped() -> None:
    now = at("14:00")
    p = package(at("15:00"), StartType.COLD)

    move_start(p, at("13:50"), now)

    assert package_start_time(p) == at("14:00")

    move_tot(p, at("14:30"), now)

    assert p.time_over_target == at("14:40")
    assert package_start_time(p) == at("14:00")


def test_start_type_change_keeps_start() -> None:
    now = at("12:00")
    alone = package(at("15:00"), StartType.COLD)

    change_start_type(alone.flights[0], StartType.IN_FLIGHT, now)

    assert alone.flights[0].start_type is StartType.IN_FLIGHT
    assert alone.time_over_target == at("14:44")
    assert package_start_time(alone) == at("14:20")

    two = package(at("15:00"), StartType.WARM, StartType.IN_FLIGHT)
    assert package_start_time(two) == at("14:28")

    change_start_type(two.flights[1], StartType.COLD, now)

    assert two.time_over_target == at("15:08")
    assert package_start_time(two) == at("14:28")


def test_added_flight_keeps_start() -> None:
    p = package(at("15:00"), StartType.IN_FLIGHT)
    before = package_start_time(p)
    assert before == at("14:36")

    FakeFlight(p, StartType.COLD)
    keep_start(p, before)

    assert p.time_over_target == at("15:16")
    assert package_start_time(p) == at("14:36")

    first = package(at("15:00"))
    FakeFlight(first, StartType.COLD)
    keep_start(first, None)

    assert first.time_over_target == at("15:00")

    asap = package(at("15:00"), StartType.IN_FLIGHT, auto_asap=True)
    before = package_start_time(asap)
    FakeFlight(asap, StartType.COLD)
    keep_start(asap, before)

    assert asap.time_over_target == at("15:00")


def test_start_type_change_with_asap_starts_now() -> None:
    now = at("12:00")
    p = package(at("15:00"), StartType.COLD, auto_asap=True)

    change_start_type(p.flights[0], StartType.IN_FLIGHT, now)

    assert p.time_over_target == at("12:24")
    assert package_start_time(p) == now


def test_start_type_change_in_locked_package_keeps_tot() -> None:
    """A package with a flight in the live mission is not re-timed: only the
    changed flight's own times move."""
    now = at("12:00")
    for asap in (False, True):
        p = package(at("15:00"), StartType.COLD, StartType.WARM, auto_asap=asap)

        change_start_type(p.flights[1], StartType.IN_FLIGHT, now, locked=True)

        assert p.flights[1].start_type is StartType.IN_FLIGHT
        assert p.time_over_target == at("15:00")
        assert p.flights[1].flight_plan.startup_time() == at("14:36")


def test_start_asap_starts_now_and_clears_asap() -> None:
    now = at("23:58")
    p = package(at("15:00"), StartType.COLD, StartType.IN_FLIGHT, auto_asap=True)

    start_asap(p, now)

    assert package_start_time(p) == now
    assert p.auto_asap is False
