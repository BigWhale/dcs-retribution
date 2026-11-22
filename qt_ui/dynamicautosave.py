"""Autosaves a dynamic campaign every 10 minutes while its mission runs.

While the campaign's own mission is followed (a link runs and
`game.live.following_own_mission`), the game is autosaved every `AUTOSAVE_S`
into its rolling autosave (`persistency.autosave`, written whole or not at
all). The time counts from when the game is set on screen or from the last
autosave here. While a flight is launching (`busy`) the autosave waits
`RETRY_S` and tries again. The timer runs on the GUI thread, so the game does
not change while it is pickled.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Slot

from game import Game, persistency
from qt_ui.companionlinkcontroller import CompanionLinkController

log = logging.getLogger(__name__)

#: Seconds between autosaves.
AUTOSAVE_S = 600.0
#: Seconds until the next try when a flight was launching.
RETRY_S = 60.0
#: How often the timer checks, in milliseconds.
CHECK_INTERVAL_MS = 10_000


class DynamicAutosave(QObject):
    def __init__(
        self,
        link: CompanionLinkController,
        busy: Callable[[], bool],
        parent: Optional[QObject] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(parent)
        self._link = link
        self._busy = busy
        self._clock = clock
        self._game: Optional[Game] = None
        # When the next autosave is due (clock seconds).
        self._due = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        # (PySide6 6.4's stub lacks the signal.)
        self._timer.timeout.connect(self.check)  # type: ignore[attr-defined]

    def set_game(self, game: Optional[Game]) -> None:
        """Follows the game on screen: a dynamic game's 10 minutes start now.

        The same game again (every game update) changes nothing.
        """
        if game is self._game:
            return
        self._game = game
        self._due = self._clock() + AUTOSAVE_S
        if game is not None and game.dynamic_campaign:
            self._timer.start()
        else:
            self._timer.stop()

    @Slot()
    def check(self) -> None:
        """Autosaves when it is due and the campaign's own mission is
        followed; while a flight is launching, tries again `RETRY_S` later."""
        game = self._game
        if game is None or not game.dynamic_campaign:
            return
        if not self._link.active or not game.live.following_own_mission:
            return
        now = self._clock()
        if now < self._due:
            return
        if self._busy():
            self._due = now + RETRY_S
            return
        started = time.perf_counter()
        saved = persistency.autosave(game)
        ms = (time.perf_counter() - started) * 1000
        self._due = now + AUTOSAVE_S
        if saved:
            log.info(f"Autosave: {persistency.dynamic_autosave_path()} in {ms:.0f} ms")
