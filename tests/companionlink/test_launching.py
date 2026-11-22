"""When a flight of the ATO is due in the live mission, and the ATO kept.

The games are stand-ins: a `dynamic_campaign` flag and the two ATOs, their
packages holding flights that carry `launched` and a flight plan's
`startup_time()`, with its `takeoff_time()` ten minutes later.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Callable

from game.ato.flight import Flight
from game.coalition import Coalition
from game.companionlink.launching import due_flights


def at(hhmm: str) -> datetime:
    hours, minutes = hhmm.split(":")
    return datetime(2026, 6, 1, int(hours), int(minutes))


class FakeFlight:
    def __init__(self, name: str, start: str, launched: bool = False) -> None:
        self.name = name
        self.launched = launched
        startup = at(start)
        takeoff = startup + timedelta(minutes=10)
        self.flight_plan = SimpleNamespace(
            startup_time=lambda: startup, takeoff_time=lambda: takeoff
        )

    def __repr__(self) -> str:
        return self.name


class BrokenFlight(FakeFlight):
    """A flight whose start time cannot be computed."""

    def __init__(self, name: str) -> None:
        super().__init__(name, "00:00")

        def fail() -> datetime:
            raise RuntimeError("no start time")

        self.flight_plan = SimpleNamespace(startup_time=fail, takeoff_time=fail)


def ato(*packages: list[FakeFlight]) -> SimpleNamespace:
    return SimpleNamespace(
        packages=[SimpleNamespace(flights=list(flights)) for flights in packages]
    )


def game(
    blue: list[list[FakeFlight]],
    red: list[list[FakeFlight]] | None = None,
    dynamic: bool = True,
    following: bool = True,
) -> Any:
    return SimpleNamespace(
        dynamic_campaign=dynamic,
        live=SimpleNamespace(following_own_mission=following),
        blue=SimpleNamespace(ato=ato(*blue)),
        red=SimpleNamespace(ato=ato(*(red or []))),
    )


def due(g: Any, now: datetime | None, receiving: bool = True, **sets: Any) -> Any:
    return due_flights(
        g,
        now,
        receiving,
        sets.get("in_progress", set()),
        sets.get("failed", set()),
    )


def test_due_at_start_time() -> None:
    """Start 14:00, takeoff 14:10: due at the start, not at the takeoff."""
    flight = FakeFlight("flight", "14:00")
    g = game([[flight]])

    assert due(g, at("13:59")) == []
    assert due(g, at("14:00")) == [flight]


def test_due_by_the_clock() -> None:
    first = FakeFlight("first", "08:00")
    second = FakeFlight("second", "08:02")
    red = FakeFlight("red", "08:00")
    g = game([[second, first]], red=[[red]])

    assert due(g, at("08:01")) == [first, red]
    assert due(g, at("08:02")) == [first, red, second]


def test_due_waits_for_receiving_link() -> None:
    first = FakeFlight("first", "08:00")
    second = FakeFlight("second", "08:02")
    g = game([[first, second]])

    assert due(g, at("08:05"), receiving=False) == []
    assert due(g, None) == []
    assert due(g, at("08:05"), receiving=True) == [first, second]


def test_nothing_due_for_a_foreign_mission() -> None:
    flight = FakeFlight("flight", "08:00")

    assert due(game([[flight]], following=False), at("08:05")) == []
    assert due(game([[flight]], following=True), at("08:05")) == [flight]


def test_flight_in_progress_not_due_again() -> None:
    first = FakeFlight("first", "08:00")
    second = FakeFlight("second", "08:00")
    g = game([[first, second]])

    assert due(g, at("08:01"), in_progress={first}) == [second]


def test_failed_flight_not_due_until_relaunched() -> None:
    first = FakeFlight("first", "08:00")
    second = FakeFlight("second", "08:00")
    g = game([[first, second]])

    assert due(g, at("08:01"), failed={first}) == [second]
    assert due(g, at("08:01"), failed=set()) == [first, second]


def test_launched_never_due() -> None:
    first = FakeFlight("first", "08:00", launched=True)
    second = FakeFlight("second", "08:00")
    g = game([[first, second]])

    assert due(g, at("09:00")) == [second]


def test_due_reads_current_ato() -> None:
    first = FakeFlight("first", "08:00")
    second = FakeFlight("second", "08:00")
    g = game([[first, second]])

    assert due(g, at("08:01")) == [first, second]
    g.blue.ato.packages[0].flights.remove(first)
    assert due(g, at("08:01")) == [second]


def test_normal_campaign_never_due() -> None:
    g = game([[FakeFlight("first", "08:00")]], dynamic=False)

    assert due(g, at("09:00")) == []


def test_flight_without_start_time_skipped() -> None:
    broken = BrokenFlight("broken")
    good = FakeFlight("good", "08:00")
    g = game([[broken, good]])

    assert due(g, at("08:01")) == [good]


def test_old_save_flight_not_launched() -> None:
    """A flight saved before `launched` existed loads as not launched."""
    flight = Flight.__new__(Flight)
    flight.__setstate__({"squadron": SimpleNamespace(settings=None), "roster": None})

    assert flight.launched is False


def record(calls: list[str], name: str) -> Callable[..., None]:
    def call(*args: Any, **kwargs: Any) -> None:
        calls.append(name)

    return call


def coalition(dynamic: bool, calls: list[str]) -> Coalition:
    c = Coalition.__new__(Coalition)
    c.game = SimpleNamespace(  # type: ignore[assignment]
        dynamic_campaign=dynamic,
        conditions=SimpleNamespace(start_time=at("08:00")),
    )
    c.ato = SimpleNamespace(clear=record(calls, "ato.clear"))  # type: ignore
    c.air_wing = SimpleNamespace(  # type: ignore
        reset=record(calls, "air_wing.reset"),
        reset_keeping_ato=record(calls, "air_wing.reset_keeping_ato"),
    )
    c.procurement_requests = SimpleNamespace(  # type: ignore
        clear=record(calls, "procurement_requests.clear")
    )
    c.transfers = SimpleNamespace(  # type: ignore
        order_airlift_assets=record(calls, "order_airlift_assets"),
        plan_transports=record(calls, "plan_transports"),
    )
    for method in (
        "refund_outstanding_orders",
        "update_transit_network",
        "plan_missions",
        "plan_procurement",
    ):
        setattr(c, method, record(calls, method))
    return c


def test_dynamic_initialize_turn_keeps_ato() -> None:
    calls: list[str] = []
    coalition(True, calls).initialize_turn(is_turn_0=False)
    assert "ato.clear" not in calls
    assert "air_wing.reset" not in calls
    assert "air_wing.reset_keeping_ato" in calls
    assert "plan_transports" in calls
    assert "plan_procurement" in calls

    calls = []
    coalition(False, calls).initialize_turn(is_turn_0=False)
    assert "ato.clear" in calls
    assert "air_wing.reset" in calls
    assert "air_wing.reset_keeping_ato" not in calls
    assert "plan_missions" in calls
