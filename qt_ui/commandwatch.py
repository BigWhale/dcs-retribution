"""Whether DCS Companion's command port answers, for a dynamic campaign's
Begin and Continue.

While a dynamic campaign is on screen, the port is asked every
`COMMAND_WATCH_MS` (`GET /v1/spawns`, `CompanionCommands.reachable`), on a
worker thread, cut short after `COMMAND_TIMEOUT_S`; the answer comes back
queued to the GUI thread. One question at a time: a slow answer is not asked
over. Without a dynamic campaign the watch is idle and its status `WAITING`.
"""

from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from game import Game
from game.companionlink.commands import CompanionCommands
from game.companionlink.linkstatus import LinkStatus
from qt_ui import liberation_install

#: How often the command port is asked, in milliseconds.
COMMAND_WATCH_MS = 5_000
#: How long one question may take, in seconds.
COMMAND_TIMEOUT_S = 3.0


class CommandWatch(QObject):
    #: The new LinkStatus: LINKED, REFUSED or WAITING.
    changed = Signal(object)

    # From the worker: (the question's epoch, its LinkStatus).
    _answered = Signal(int, object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._status = LinkStatus.WAITING
        # Counts the watches; an answer to an earlier one is dropped.
        self._epoch = 0
        self._asking = False
        self._timer = QTimer(self)
        # (PySide6 6.4's stub lacks the signal.)
        self._timer.timeout.connect(self._ask)  # type: ignore[attr-defined]
        # Queued: emitted on the worker's thread. (PySide6 6.4's stub types
        # the connection type wrong.)
        queued = Qt.ConnectionType.QueuedConnection
        self._answered.connect(self._on_answered, queued)  # type: ignore[arg-type]

    @property
    def status(self) -> LinkStatus:
        """The command port's last answer; WAITING before the first."""
        return self._status

    def set_game(self, game: Optional[Game]) -> None:
        """Watches while `game` is a dynamic campaign, asking at once; stops
        otherwise."""
        if game is None or not game.dynamic_campaign:
            self.stop()
            return
        if self._timer.isActive():
            return
        self._timer.start(COMMAND_WATCH_MS)
        self._ask()

    def stop(self) -> None:
        """Stops watching; an answer still to come is dropped."""
        self._timer.stop()
        self._epoch += 1
        self._asking = False
        self._set_status(LinkStatus.WAITING)

    @Slot()
    def _ask(self) -> None:
        if self._asking:
            return
        self._asking = True
        epoch = self._epoch
        commands = CompanionCommands(
            liberation_install.companion_command_address(),
            liberation_install.companion_command_token(),
            timeout=COMMAND_TIMEOUT_S,
        )

        def ask() -> None:
            status = commands.reachable()
            try:
                self._answered.emit(epoch, status)
            except RuntimeError:
                pass  # the watch is gone (its window closed)

        threading.Thread(target=ask, name="command watch", daemon=True).start()

    @Slot(int, object)
    def _on_answered(self, epoch: int, status: LinkStatus) -> None:
        if epoch != self._epoch:
            return
        self._asking = False
        self._set_status(status)

    def _set_status(self, status: LinkStatus) -> None:
        if status is self._status:
            return
        self._status = status
        self.changed.emit(status)
