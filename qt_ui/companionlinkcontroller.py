"""The link to DCS Companion for the game on screen: one link per dynamic game.

The link's callbacks run on its thread. They only emit queued signals, which
reach this object on the GUI thread; they never wait for the GUI thread, so
`stop()` (which holds the callbacks' lock while it joins the thread) cannot
deadlock. Each queued event carries the link it came from, and an event of a
link that has since been stopped is dropped.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from PySide6.QtCore import QObject, Qt, Signal, Slot

from game import Game
from game.companionlink.link import CompanionLink, LinkStatus
from game.companionlink.livestate import LiveStateContext
from game.companionlink.snapshot import LiveSnapshot
from game.server import EventStream
from game.sim import GameUpdateEvents
from qt_ui import liberation_install

log = logging.getLogger(__name__)

#: How far the live mission's start may be from the game's before a warning.
START_TOLERANCE_S = 60.0


class CompanionLinkController(QObject):
    #: A LiveSnapshot, on the GUI thread.
    snapshot_received = Signal(object)
    #: A LinkStatus, on the GUI thread; also when a link starts or stops.
    status_changed = Signal(object)

    # From the link's thread: (the link, the snapshot or status).
    _link_snapshot = Signal(object, object)
    _link_status = Signal(object, object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._game: Optional[Game] = None
        self._link: Optional[CompanionLink] = None
        self._status = LinkStatus.WAITING
        self._snapshot: Optional[LiveSnapshot] = None
        # The missions (boot, session) whose start was already warned about.
        self._start_warned: set[tuple[str, int]] = set()
        # Queued: they are emitted on the link's thread. (PySide6 6.4's stub
        # types the connection type wrong.)
        queued = Qt.ConnectionType.QueuedConnection
        self._link_snapshot.connect(self._on_link_snapshot, queued)  # type: ignore[arg-type]
        self._link_status.connect(self._on_link_status, queued)  # type: ignore[arg-type]

    @property
    def active(self) -> bool:
        """Whether a link is running."""
        return self._link is not None

    def set_game(self, game: Game | None) -> None:
        """Follows the game on screen.

        The same game again (every game update) keeps its link. Another game,
        or none, stops the link and clears the live state; a dynamic campaign
        that has begun (turn > 0) starts a new one.
        """
        if game is not None and game is self._game and self._link is not None:
            return
        self.stop()
        self._game = game
        if game is not None and game.dynamic_campaign and game.turn > 0:
            self.start(game)

    def start(self, game: Game) -> None:
        """Starts the link for a dynamic game; nothing if it already runs."""
        if not game.dynamic_campaign:
            return
        if self._link is not None and game is self._game:
            return
        self.stop()
        self._game = game
        self._status = LinkStatus.WAITING
        self._snapshot = None

        def on_snapshot(snapshot: LiveSnapshot) -> None:
            self._link_snapshot.emit(link, snapshot)

        def on_status(status: LinkStatus) -> None:
            self._link_status.emit(link, status)

        link = CompanionLink(
            liberation_install.companion_address(),
            liberation_install.companion_token(),
            on_snapshot=on_snapshot,
            on_status=on_status,
        )
        self._link = link
        link.start()
        self.status_changed.emit(self._status)

    def stop(self) -> None:
        """Stops the link and joins its thread; clears the live state and
        resets the mission clock."""
        link = self._link
        self._link = None
        self._snapshot = None
        self._status = LinkStatus.WAITING
        self._start_warned.clear()
        LiveStateContext.set(None)  # resets the clock too
        if link is None:
            return
        link.stop()
        if link.is_alive():
            log.warning("Companion link: the thread did not end within its wait")
        self.status_changed.emit(self._status)

    def status_text(self) -> str:
        if self._status is LinkStatus.REFUSED:
            return "Companion: refused (token)"
        if self._status is LinkStatus.RECEIVING and self._snapshot is not None:
            minute = self._snapshot.minute
            units = self._snapshot.unit_count
            return f"Companion: minute {minute}, {units:,} units"
        if self._status in (LinkStatus.LINKED, LinkStatus.RECEIVING):
            return "Companion: linked, waiting for the first snapshot"
        return "Companion: waiting for server"

    @Slot(object, object)
    def _on_link_status(self, link: CompanionLink, status: LinkStatus) -> None:
        if link is not self._link:
            return
        self._status = status
        self.status_changed.emit(status)

    @Slot(object, object)
    def _on_link_snapshot(self, link: CompanionLink, snapshot: LiveSnapshot) -> None:
        if link is not self._link:
            return
        self._snapshot = snapshot
        LiveStateContext.set(snapshot)
        LiveStateContext.clock().update(snapshot, time.monotonic())
        self._check_mission_start(snapshot)
        log.info(
            "Companion snapshot: minute %d, %d units, %d statics, %d bytes",
            snapshot.minute,
            snapshot.unit_count,
            len(snapshot.units) - snapshot.unit_count,
            snapshot.size,
        )
        EventStream.put_nowait(GameUpdateEvents().update_live_units(snapshot))
        self.snapshot_received.emit(snapshot)

    def _check_mission_start(self, snapshot: LiveSnapshot) -> None:
        """Warns once per mission when its start is not the game's.

        The snapshot's time is the truth; the clock follows it either way.
        """
        if snapshot.abs_time is None or self._game is None:
            return
        start = snapshot.abs_time - timedelta(seconds=snapshot.model_time)
        mission = (snapshot.boot, snapshot.session)
        if mission in self._start_warned:
            return
        expected: datetime = self._game.conditions.start_time
        off = (start - expected).total_seconds()
        if abs(off) <= START_TOLERANCE_S:
            return
        self._start_warned.add(mission)
        log.warning(
            "Companion: the live mission started at %s, the game's start is %s "
            "(%+.0f s); following the live mission",
            start.isoformat(sep=" ", timespec="seconds"),
            expected.isoformat(sep=" ", timespec="seconds"),
            off,
        )
