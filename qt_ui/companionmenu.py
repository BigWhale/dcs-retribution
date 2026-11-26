"""The Companion menu: DCS Companion's service, seen and controlled.

The status line (`Companion: …`) is asked every 5 s and whenever the menu
opens, on a thread of its own, and reaches the menu through a queued signal;
Restart and Turn off also run on a thread (a restart may wait 10 s for the
companion to stop). Restart, Turn off or on, and Show log are offered only
where the companion's folder is (`CompanionService.managed`); elsewhere the
menu shows the status alone. Turning the companion off asks first while a
dynamic campaign follows its mission.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QAction, QFont, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from game.companionlink.service import CompanionService, ServiceState, ServiceStatus
from qt_ui import liberation_install

#: How often the status is asked, in ms.
REFRESH_MS = 5000
#: How often an open log window reads the log again, in ms.
LOG_REFRESH_MS = 2000
#: Lines an open log window shows.
LOG_LINES = 500


class CompanionMenu(QMenu):
    status_ready = Signal(object)

    def __init__(
        self,
        parent: QWidget,
        following: Callable[[], bool],
        service: Optional[CompanionService] = None,
    ) -> None:
        super().__init__("&Companion", parent)
        self._following = following
        self._given = service
        self._asking = False
        self._lock = threading.Lock()
        self._log_window: Optional[CompanionLogWindow] = None

        self.status_action = QAction("Companion: …", self)
        self.status_action.setEnabled(False)
        self.restart_action = QAction("Restart", self)
        self.restart_action.setEnabled(False)
        self.restart_action.triggered.connect(self._restart)
        self.power_action = QAction("Turn off", self)
        self.power_action.triggered.connect(self._power)
        self.log_action = QAction("Show log", self)
        self.log_action.triggered.connect(self._show_log)
        self.addAction(self.status_action)
        self.addSeparator()
        self.addAction(self.restart_action)
        self.addAction(self.power_action)
        self.addAction(self.log_action)
        self._show_managed(self.service().managed)

        self.status_ready.connect(self._show_status)
        self.aboutToShow.connect(self.refresh)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)
        self._last: Optional[ServiceStatus] = None

    def service(self) -> CompanionService:
        """The given service, or one from the preferences as they are now."""
        if self._given is not None:
            return self._given
        return CompanionService(
            Path(liberation_install.companion_home()),
            liberation_install.companion_address(),
            liberation_install.companion_token(),
        )

    def refresh(self) -> None:
        """Asks the status once, off the GUI thread; one ask at a time."""
        with self._lock:
            if self._asking:
                return
            self._asking = True
        service = self.service()

        def ask() -> None:
            try:
                status = service.status()
            finally:
                with self._lock:
                    self._asking = False
            self.status_ready.emit(status)

        threading.Thread(target=ask, name="companion-status", daemon=True).start()

    def _show_managed(self, managed: bool) -> None:
        for action in (self.restart_action, self.power_action, self.log_action):
            action.setVisible(managed)

    def _show_status(self, status: ServiceStatus) -> None:
        self._last = status
        self.status_action.setText(f"Companion: {status.text()}")
        self._show_managed(status.managed)
        off = status.state is ServiceState.OFF
        self.restart_action.setEnabled(status.managed and not off)
        self.power_action.setText("Turn on" if off else "Turn off")

    def _in_background(self, work: Callable[[], object]) -> None:
        def run() -> None:
            work()
            self.refresh()

        threading.Thread(target=run, name="companion-control", daemon=True).start()

    def _restart(self) -> None:
        self.status_action.setText("Companion: restarting")
        self._in_background(self.service().restart)

    def _power(self) -> None:
        service = self.service()
        if self._last is not None and self._last.state is ServiceState.OFF:
            service.turn_on()
            self.refresh()
            return
        if self._following():
            answer = QMessageBox.question(
                self.parentWidget(),
                "Turn the companion off?",
                "The campaign follows its mission through the companion. "
                "Turned off, the companion stops, nothing on the server is "
                "followed, and it stays off until it is turned on again.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.status_action.setText("Companion: turning off")
        self._in_background(service.turn_off)

    def _show_log(self) -> None:
        if self._log_window is None:
            self._log_window = CompanionLogWindow(self.service, self.parentWidget())
        self._log_window.show()
        self._log_window.raise_()
        self._log_window.activateWindow()


class CompanionLogWindow(QDialog):
    """The end of `companion.log`, read again every 2 s while open."""

    def __init__(
        self, service: Callable[[], CompanionService], parent: Optional[QWidget]
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("DCS Companion log")
        self.resize(1000, 600)
        self._service = service
        self.text = QPlainTextEdit(self)
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setFont(QFont("Monospace"))
        layout = QVBoxLayout(self)
        layout.addWidget(self.text)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.reload)

    def reload(self) -> None:
        tail = self._service().log_tail(LOG_LINES)
        if tail != self.text.toPlainText():
            self.text.setPlainText(tail)
            self.text.moveCursor(QTextCursor.MoveOperation.End)

    def showEvent(self, event: object) -> None:  # type: ignore[override]
        self.reload()
        self._timer.start(LOG_REFRESH_MS)
        super().showEvent(event)  # type: ignore[arg-type]

    def hideEvent(self, event: object) -> None:  # type: ignore[override]
        self._timer.stop()
        super().hideEvent(event)  # type: ignore[arg-type]
