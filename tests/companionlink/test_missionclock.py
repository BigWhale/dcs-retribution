"""The mission clock: the live mission's time, between and across snapshots."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pytest

from game.companionlink.missionclock import MissionClock
from game.companionlink.snapshot import LiveSnapshot


def snapshot(
    abs_time: Optional[datetime],
    model_time: float,
    boot: str = "b1",
    session: int = 1,
) -> LiveSnapshot:
    return LiveSnapshot(
        minute=int(model_time // 60),
        boot=boot,
        session=session,
        model_time=model_time,
        units=(),
        size=0,
        abs_time=abs_time,
    )


def test_now_extrapolates_between_snapshots() -> None:
    clock = MissionClock()
    clock.update(snapshot(datetime(2005, 7, 1, 8, 0, 0), 600), received=100.0)
    assert clock.now(130.0) == datetime(2005, 7, 1, 8, 0, 30)
    assert clock.model_time(130.0) == pytest.approx(630)
    assert clock.mission_start() == datetime(2005, 7, 1, 7, 50, 0)


def test_next_snapshot_corrects() -> None:
    clock = MissionClock()
    clock.update(snapshot(datetime(2005, 7, 1, 8, 0, 0), 600), received=100.0)
    clock.update(snapshot(datetime(2005, 7, 1, 8, 1, 5), 665), received=160.0)
    assert clock.now(160.0) == datetime(2005, 7, 1, 8, 1, 5)


def test_no_time_before_snapshot() -> None:
    clock = MissionClock()
    assert clock.now() is None
    assert clock.mission_start() is None
    assert clock.model_time() is None
    clock.update(snapshot(None, 600), received=100.0)
    assert clock.now() is None
    assert clock.mission_start() is None


def test_new_mission_moves_back() -> None:
    clock = MissionClock()
    clock.update(snapshot(datetime(2005, 7, 1, 8, 1, 5), 665), received=160.0)
    clock.update(
        snapshot(datetime(2005, 7, 1, 7, 50, 10), 10, boot="b2", session=1),
        received=200.0,
    )
    assert clock.now(200.0) == datetime(2005, 7, 1, 7, 50, 10)
    assert clock.model_time(200.0) == pytest.approx(10)
    assert clock.mission_start() == datetime(2005, 7, 1, 7, 50, 0)


def test_reset() -> None:
    clock = MissionClock()
    clock.update(snapshot(datetime(2005, 7, 1, 8, 0, 0), 600), received=100.0)
    clock.reset()
    assert clock.now() is None
    assert clock.mission_start() is None


def test_held_clock_stands_still() -> None:
    clock = MissionClock()
    clock.update(snapshot(datetime(2005, 7, 1, 8, 0, 0), 600), received=100.0)
    at_hold = clock.now(130.0)
    model_at_hold = clock.model_time(130.0)
    clock.hold(130.0)
    assert clock.now(500.0) == at_hold
    assert clock.model_time(500.0) == pytest.approx(model_at_hold)
    clock.update(snapshot(datetime(2005, 7, 1, 8, 20, 0), 1800), received=600.0)
    assert clock.now(610.0) == datetime(2005, 7, 1, 8, 20, 10)
    assert clock.model_time(610.0) == pytest.approx(1810)


def test_held_twice_stands_at_first() -> None:
    clock = MissionClock()
    clock.update(snapshot(datetime(2005, 7, 1, 8, 0, 0), 600), received=100.0)
    at_first = clock.now(130.0)
    clock.hold(130.0)
    clock.hold(200.0)
    assert clock.now(500.0) == at_first
