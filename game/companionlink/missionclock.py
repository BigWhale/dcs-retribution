"""The live mission's time, for a dynamic campaign's "now".

Each snapshot carries the mission's absolute time and model time. The clock
keeps the newest of them with the moment it arrived (`time.monotonic()`) and
runs on with the wall clock between snapshots; the next snapshot corrects it.
A new mission (DCS restarted, mission reloaded) moves it to that mission's
time, backwards too.

Updated on the GUI thread, read from any: the anchor is one tuple, swapped
whole.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Optional

from game.companionlink.snapshot import LiveSnapshot

#: The newest snapshot's absolute time, its model time, and when it arrived.
_Anchor = tuple[datetime, float, float]


class MissionClock:
    def __init__(self) -> None:
        self._anchor: Optional[_Anchor] = None

    def update(self, snapshot: LiveSnapshot, received: float) -> None:
        """Follows a snapshot received at `received` (time.monotonic()).

        A snapshot without an absolute time leaves the clock as it was.
        """
        if snapshot.abs_time is None:
            return
        self._anchor = (snapshot.abs_time, snapshot.model_time, received)

    def now(self, wall: Optional[float] = None) -> Optional[datetime]:
        """The mission's time at `wall` (default now); None before a snapshot."""
        anchor = self._anchor
        if anchor is None:
            return None
        abs_time, _, received = anchor
        return abs_time + timedelta(seconds=_wall(wall) - received)

    def mission_start(self) -> Optional[datetime]:
        """The mission's absolute time at model time 0; None before a snapshot."""
        anchor = self._anchor
        if anchor is None:
            return None
        abs_time, model_time, _ = anchor
        return abs_time - timedelta(seconds=model_time)

    def model_time(self, wall: Optional[float] = None) -> Optional[float]:
        """The mission's model time at `wall` (default now); None before a snapshot."""
        anchor = self._anchor
        if anchor is None:
            return None
        _, model_time, received = anchor
        return model_time + (_wall(wall) - received)

    def reset(self) -> None:
        """Forgets the mission: no time until the next snapshot."""
        self._anchor = None


def _wall(wall: Optional[float]) -> float:
    return time.monotonic() if wall is None else wall


def dated_time(t: datetime, now: Optional[datetime]) -> str:
    """`t` as `23:58` on `now`'s day (or with no `now`), as `14 Jul 00:12` on
    another day."""
    if now is None or t.date() == now.date():
        return f"{t:%H:%M}"
    return f"{t:%d %b %H:%M}"
