"""The link to DCS Companion for the game on screen: one link per dynamic game.

The link's callbacks run on its thread. They only emit queued signals, which
reach this object on the GUI thread; they never wait for the GUI thread, so
`stop()` (which holds the callbacks' lock while it joins the thread) cannot
deadlock. Each queued event carries the link it came from, and an event of a
link that has since been stopped is dropped.

A dynamic campaign follows only its own mission (`game.live.decide`, by the
mission's marker): its snapshots set the live state and the mission clock,
the live map draws them, and the record keeps the newest as its tick. Another
mission, a link that is not receiving, or no snapshot for `STALE_S` is not
followed: the clock holds at its last time, the live map is cleared once, and
the snapshot is not kept. A mission of the campaign's that is not followed
(loaded again from its start, or not picked up) is treated the same way. The
event log's entries go to `events_received` either way; `LiveLosses` decides
what to apply. When the game's verdicts change under the link (a restore
retires the campaign's missions), `recheck` stops following at once.

A loaded save (`start(game, pickup=True)`) runs the pick-up check at the first
snapshot, before the save's verdicts decide anything: it asks the command port
for the mission's spawns (`GET /v1/spawns`, on the GUI thread, cut short after
`PICKUP_TIMEOUT_S`) and lets `game.live.pick_up` decide. While the command port
does not answer, or describes another mission than the snapshot, nothing is
followed and the check runs again at the next snapshot. Once it decides, the
snapshot goes on to `decide` as any other; a mission picked up shows how long
after the save it was picked up (`pickup_note`) while it is followed.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from game import Game
from game.companionlink.commands import CommandError, CompanionCommands
from game.companionlink.eventlog import EventBatch
from game.companionlink.link import CompanionLink
from game.companionlink.linkstatus import LinkStatus
from game.companionlink.liverecord import (
    MissionVerdict,
    companion_status,
    picked_up_note,
)
from game.companionlink.livestate import LiveStateContext
from game.companionlink.snapshot import LiveSnapshot
from game.server import EventStream
from game.sim import GameUpdateEvents
from qt_ui import liberation_install

log = logging.getLogger(__name__)

#: How far the live mission's start may be from the game's before a warning.
START_TOLERANCE_S = 60.0
#: With no snapshot for this long (monotonic seconds) the server's mission has
#: stopped or DCS is down; snapshots come every minute.
STALE_S = 150.0
#: How often the link is checked for staleness, in milliseconds.
STALE_CHECK_MS = 10_000
#: How long the pick-up check waits for the command port's spawns, in
#: seconds; the GUI thread waits on it.
PICKUP_TIMEOUT_S = 5.0

#: What the live map is sent when the mission stops being followed.
_EMPTY = LiveSnapshot(minute=0, boot="", session=0, model_time=0.0, units=(), size=0)


class CompanionLinkController(QObject):
    #: A LiveSnapshot, on the GUI thread.
    snapshot_received = Signal(object)
    #: A LinkStatus, on the GUI thread; also when a link starts or stops, or
    #: goes stale.
    status_changed = Signal(object)
    #: An EventBatch of the event log, on the GUI thread.
    events_received = Signal(object)
    #: The MissionVerdict of a mission newly current, on the GUI thread.
    verdict_changed = Signal(object)

    # From the link's thread: (the link, the snapshot, status or batch).
    _link_snapshot = Signal(object, object)
    _link_status = Signal(object, object)
    _link_events = Signal(object, object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._game: Optional[Game] = None
        self._link: Optional[CompanionLink] = None
        self._status = LinkStatus.WAITING
        self._snapshot: Optional[LiveSnapshot] = None
        # The missions (boot, session) whose start was already logged.
        self._start_logged: set[tuple[str, int]] = set()
        # When the newest snapshot of any mission arrived (time.monotonic()).
        self._received_at: Optional[float] = None
        # No snapshot for STALE_S.
        self._stale = False
        # Whether the newest snapshot was followed.
        self._followed = False
        # The mission and verdict last emitted by verdict_changed.
        self._verdict: Optional[tuple[tuple[str, int], MissionVerdict]] = None
        # Whether the game is a loaded save that runs the pick-up check.
        self._pickup = False
        # The pick-up check could not ask the command port at the last
        # snapshot.
        self._awaiting_port = False
        # The mission picked up and its note.
        self._note: Optional[tuple[tuple[str, int], str]] = None
        # The mission file a load waits for, and the one that did not load.
        self._loading: Optional[str] = None
        self._load_failed: Optional[str] = None
        self._stale_timer = QTimer(self)
        self._stale_timer.setInterval(STALE_CHECK_MS)
        # (PySide6 6.4's stub lacks the signal.)
        self._stale_timer.timeout.connect(self._check_stale)  # type: ignore[attr-defined]
        # Queued: they are emitted on the link's thread. (PySide6 6.4's stub
        # types the connection type wrong.)
        queued = Qt.ConnectionType.QueuedConnection
        self._link_snapshot.connect(self._on_link_snapshot, queued)  # type: ignore[arg-type]
        self._link_status.connect(self._on_link_status, queued)  # type: ignore[arg-type]
        self._link_events.connect(self._on_link_events, queued)  # type: ignore[arg-type]

    @property
    def active(self) -> bool:
        """Whether a link is running."""
        return self._link is not None

    def set_game(self, game: Game | None) -> None:
        """Follows the game on screen.

        The same game again (every game update) keeps its link. Another game,
        or none, stops the link and clears the live state; a dynamic campaign
        that has begun (turn > 0) starts a new one, with the pick-up check
        while the game awaits one (a loaded save).
        """
        if game is not None and game is self._game and self._link is not None:
            return
        self.stop()
        self._game = game
        if game is not None and game.dynamic_campaign and game.turn > 0:
            self.start(game, pickup=game.live.awaiting_pickup)

    def start(self, game: Game, pickup: bool = False) -> None:
        """Starts the link for a dynamic game; nothing if it already runs.

        `pickup`: the game is a loaded save; its first snapshot runs the
        pick-up check. Without it, a game still awaiting a pick-up follows
        nothing (Begin and Continue end the pick-up before they start the
        link)."""
        if not game.dynamic_campaign:
            return
        if self._link is not None and game is self._game:
            return
        self.stop()
        self._game = game
        self._pickup = pickup
        self._status = LinkStatus.WAITING
        self._snapshot = None
        self._forget_following()

        def on_snapshot(snapshot: LiveSnapshot) -> None:
            self._link_snapshot.emit(link, snapshot)

        def on_status(status: LinkStatus) -> None:
            self._link_status.emit(link, status)

        def on_events(batch: EventBatch) -> None:
            self._link_events.emit(link, batch)

        link = CompanionLink(
            liberation_install.companion_address(),
            liberation_install.companion_token(),
            on_snapshot=on_snapshot,
            on_status=on_status,
            on_events=on_events,
            events_after=game.live.cursor,
        )
        self._link = link
        link.start()
        self._stale_timer.start()
        self.status_changed.emit(self._status)

    def stop(self) -> None:
        """Stops the link and joins its thread; clears the live state and
        resets the mission clock."""
        link = self._link
        self._link = None
        self._snapshot = None
        self._status = LinkStatus.WAITING
        self._pickup = False
        self._start_logged.clear()
        self._stale_timer.stop()
        self._forget_following()
        LiveStateContext.set(None)  # resets the clock too
        if link is None:
            return
        link.stop()
        if link.is_alive():
            log.warning("Companion link: the thread did not end within its wait")
        self.status_changed.emit(self._status)

    @property
    def pickup_note(self) -> Optional[str]:
        """How long after the save the running mission was picked up; None
        when it was not, or the mission followed has changed since."""
        return self._note[1] if self._note is not None else None

    def set_loading(self, loading: Optional[str], failed: Optional[str]) -> None:
        """The mission file a load waits for, and the one that did not load
        (None for neither); the status shows them."""
        self._loading = loading
        self._load_failed = failed
        self.status_changed.emit(self._status)

    def status_text(self) -> str:
        game = self._game
        live = game.live if game is not None else None
        return companion_status(
            self._status,
            self._snapshot,
            self._stale,
            live.current_verdict if live is not None else None,
            note=self.pickup_note,
            reason=live.moved_on_reason if live is not None else None,
            loading=self._loading,
            load_failed=self._load_failed,
            # Only while the gate is on: Continue ends it between snapshots.
            awaiting=self._awaiting_port and live is not None and live.awaiting_pickup,
        )

    def recheck(self) -> None:
        """The game's verdicts changed (a restore retired the campaign's
        missions): stops following at once when the current mission is no
        longer the campaign's own, as a stale link does, not at its next
        snapshot."""
        game = self._game
        if game is not None and not game.live.following_own_mission:
            self._unfollow()
        self.status_changed.emit(self._status)

    def _forget_following(self) -> None:
        self._received_at = None
        self._stale = False
        self._followed = False
        self._verdict = None
        self._awaiting_port = False
        self._note = None

    def _unfollow(self) -> None:
        """Stops following the live mission: the clock holds at its last time
        and the live map is cleared, once on the change."""
        if not self._followed:
            return
        self._followed = False
        LiveStateContext.clock().hold()
        LiveStateContext.set(_EMPTY)
        EventStream.put_nowait(GameUpdateEvents().update_live_units(_EMPTY))

    def _lose(self) -> None:
        """No mission is current: the link is stale or not receiving."""
        if self._game is not None:
            self._game.live.lose()
        self._unfollow()

    @Slot()
    def _check_stale(self) -> None:
        """Marks the link stale when the newest snapshot of any mission is
        older than STALE_S."""
        received = self._received_at
        if self._link is None or self._stale or received is None:
            return
        if time.monotonic() - received <= STALE_S:
            return
        self._stale = True
        log.info("Companion: no snapshot for %.0f s; waiting for a mission", STALE_S)
        self._lose()
        self.status_changed.emit(self._status)

    @Slot(object, object)
    def _on_link_status(self, link: CompanionLink, status: LinkStatus) -> None:
        if link is not self._link:
            return
        self._status = status
        if status is not LinkStatus.RECEIVING:
            self._lose()
        self.status_changed.emit(status)

    @Slot(object, object)
    def _on_link_events(self, link: CompanionLink, batch: EventBatch) -> None:
        if link is not self._link:
            return
        self.events_received.emit(batch)

    @Slot(object, object)
    def _on_link_snapshot(self, link: CompanionLink, snapshot: LiveSnapshot) -> None:
        if link is not self._link or self._game is None:
            return
        game = self._game
        self._snapshot = snapshot
        self._received_at = time.monotonic()
        was_stale = self._stale
        self._stale = False
        mission = (snapshot.boot, snapshot.session)
        if self._note is not None and self._note[0] != mission:
            self._note = None
        if self._pickup and game.live.awaiting_pickup:
            if self._status is not LinkStatus.RECEIVING or not self._pick_up(
                game, snapshot
            ):
                self._unfollow()
                self.status_changed.emit(self._status)
                return
        verdict = game.live.decide(snapshot)
        if self._verdict != (mission, verdict):
            self._verdict = (mission, verdict)
            self.verdict_changed.emit(verdict)
        if was_stale:
            self.status_changed.emit(self._status)
        if not verdict.followed or self._status is not LinkStatus.RECEIVING:
            self._unfollow()
            return
        self._followed = True
        game.live.keep(snapshot)
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

    def _pick_up(self, game: Game, snapshot: LiveSnapshot) -> bool:
        """The pick-up check on the snapshot's mission; whether it decided.

        It does not while the command port cannot be asked (the status says
        so) or describes another mission than the snapshot."""
        commands = CompanionCommands(
            liberation_install.companion_command_address(),
            liberation_install.companion_command_token(),
            timeout=PICKUP_TIMEOUT_S,
        )
        try:
            view = commands.spawns()
        except CommandError as e:
            if not self._awaiting_port:
                log.warning(
                    "Pick-up: cannot ask the command port (%s); checking again "
                    "at the next snapshot",
                    e,
                )
            self._awaiting_port = True
            return False
        self._awaiting_port = False
        pickup = game.live.pick_up(snapshot, view)
        if not pickup.decided:
            log.info(
                "Pick-up: the command port describes boot %s session %d, the "
                "snapshot boot %s session %d; checking again at the next snapshot",
                view.boot,
                view.session,
                snapshot.boot,
                snapshot.session,
            )
            return False
        if pickup.picked:
            self._note = (
                (snapshot.boot, snapshot.session),
                picked_up_note(pickup.seconds),
            )
        return True

    def _check_mission_start(self, snapshot: LiveSnapshot) -> None:
        """Logs once per mission followed when its start is not the game's.

        The snapshot's time is the truth; the clock follows it either way.
        Whether a mission is the campaign's is its marker's to say.
        """
        if snapshot.abs_time is None or self._game is None:
            return
        start = snapshot.abs_time - timedelta(seconds=snapshot.model_time)
        mission = (snapshot.boot, snapshot.session)
        if mission in self._start_logged:
            return
        expected: datetime = self._game.conditions.start_time
        off = (start - expected).total_seconds()
        if abs(off) <= START_TOLERANCE_S:
            return
        self._start_logged.add(mission)
        log.info(
            "Companion: the live mission started at %s, the game's start is %s "
            "(%+.0f s); following the live mission",
            start.isoformat(sep=" ", timespec="seconds"),
            expected.isoformat(sep=" ", timespec="seconds"),
            off,
        )
