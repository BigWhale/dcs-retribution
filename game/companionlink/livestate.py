"""The newest snapshot of the running mission, for whoever draws it, and the
mission clock that follows it.

Memory only: set on the GUI thread when a snapshot arrives, cleared when the
game is swapped or the link stops, never saved with the game.
"""

from __future__ import annotations

from typing import Optional

from game.companionlink.missionclock import MissionClock
from game.companionlink.snapshot import LiveSnapshot


class LiveStateContext:
    _snapshot: Optional[LiveSnapshot] = None
    _clock = MissionClock()

    @classmethod
    def set(cls, snapshot: LiveSnapshot | None) -> None:
        """The newest snapshot; None also resets the clock."""
        cls._snapshot = snapshot
        if snapshot is None:
            cls._clock.reset()

    @classmethod
    def get(cls) -> LiveSnapshot | None:
        return cls._snapshot

    @classmethod
    def clock(cls) -> MissionClock:
        """The mission clock, one per process; the controller feeds it."""
        return cls._clock
