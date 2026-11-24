"""Autosaves a dynamic campaign every 10 minutes while its mission runs,
after each launch and on quit.

While the campaign's own mission is followed (a link runs and
`game.live.following_own_mission`), the game is autosaved every `AUTOSAVE_S`
into its rolling autosave (`persistency.autosave`, written whole or not at
all). The time counts from when the game is set on screen or from the last
autosave here. While a flight is launching (`busy`) the autosave waits
`RETRY_S` and tries again. The timer runs on the GUI thread, so the game does
not change while it is pickled.

A campaign that has begun is also autosaved at once, whatever mission is
followed: after each launch (`autosave_now`; skipped while another flight is
launching, the timer then trying `RETRY_S` later) and when the main window
closes (`autosave_on_quit`; a launch on its way is waited for up to
`QUIT_WAIT_S`, then the autosave is written anyway). So an accidental close
finds an autosave the running mission has not moved past.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, QTimer, Slot

from game import Game, persistency
from qt_ui.companionlinkcontroller import CompanionLinkController

log = logging.getLogger(__name__)

#: Seconds between autosaves.
AUTOSAVE_S = 600.0
#: Seconds until the next try when a flight was launching.
RETRY_S = 60.0
#: How often the timer checks, in milliseconds.
CHECK_INTERVAL_MS = 10_000
#: On quit, how long the autosave waits for a flight launching, in seconds.
QUIT_WAIT_S = 10.0
#: How often the wait on quit looks again, in seconds.
QUIT_POLL_S = 0.05


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
        self._autosave(game)

    @Slot()
    def autosave_now(self) -> bool:
        """Autosaves now a dynamic campaign that has begun, whatever mission is
        followed (after a launch); the 10 minutes count from here. Not while a
        flight is launching: the timer tries again `RETRY_S` later. Whether
        the autosave was written."""
        game = self._begun()
        if game is None:
            return False
        if self._busy():
            self._due = min(self._due, self._clock() + RETRY_S)
            return False
        return self._autosave(game)

    def autosave_on_quit(self, wait_s: Optional[float] = None) -> bool:
        """Autosaves a dynamic campaign that has begun as the window closes,
        whatever mission is followed. A flight launching is waited for up to
        `wait_s` (default `QUIT_WAIT_S`), the Qt events processed meanwhile
        (but the user's input), then the autosave is written anyway. Whether
        it was written."""
        game = self._begun()
        if game is None:
            return False
        limit = QUIT_WAIT_S if wait_s is None else wait_s
        end = time.monotonic() + limit
        while self._busy() and time.monotonic() < end:
            QCoreApplication.processEvents(
                QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
            )
            time.sleep(min(QUIT_POLL_S, max(end - time.monotonic(), 0.0)))
        if self._busy():
            log.warning(
                f"Autosave: a flight is still launching after {limit:g} s; "
                "saving anyway"
            )
        return self._autosave(game)

    def _begun(self) -> Optional[Game]:
        """The game on screen when it is a dynamic campaign that has begun."""
        game = self._game
        if game is None or not game.dynamic_campaign or game.turn == 0:
            return None
        return game

    def _autosave(self, game: Game) -> bool:
        """Writes the autosave and logs it; the next is due `AUTOSAVE_S` from
        now."""
        started = time.perf_counter()
        saved = persistency.autosave(game)
        ms = (time.perf_counter() - started) * 1000
        self._due = self._clock() + AUTOSAVE_S
        if saved:
            log.info(f"Autosave: {persistency.dynamic_autosave_path()} in {ms:.0f} ms")
        return saved
