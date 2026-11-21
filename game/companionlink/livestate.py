"""The newest snapshot of the running mission, for whoever draws it.

Memory only: set on the GUI thread when a snapshot arrives, cleared when the
game is swapped or the link stops, never saved with the game.
"""

from __future__ import annotations

from typing import Optional

from game.companionlink.snapshot import LiveSnapshot


class LiveStateContext:
    _snapshot: Optional[LiveSnapshot] = None

    @classmethod
    def set(cls, snapshot: LiveSnapshot | None) -> None:
        cls._snapshot = snapshot

    @classmethod
    def get(cls) -> LiveSnapshot | None:
        return cls._snapshot
