"""Launches a dynamic campaign's flights into the live mission.

Every second, while the companion link is receiving, the launcher asks
`due_flights` which flights of the ATO are due at the mission clock's time
and launches each: its group is generated on the GUI thread (about 0.2 s),
made fit for the live mission, then sent to DCS Companion's command port and
followed to `spawned` on a worker thread. The result comes back to the GUI
thread by a queued signal: a spawned flight is marked launched ("In Flight"),
a failed one waits for the user to press Launch again.

Each flight is launched at most once at a time; several flights may be on
their way at once. The ids and names handed out belong to one mission (the
snapshot's boot and session) and start afresh with a new mission.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from game import Game
from game.ato.flight import Flight
from game.companionlink.commands import CompanionCommands, SpawnRequest
from game.companionlink.flightspawn import (
    IdAllocator,
    generate_flight_table,
    prepare_spawn,
)
from game.companionlink.launching import due_flights
from game.companionlink.link import LinkStatus
from game.companionlink.livestate import LiveStateContext
from qt_ui import liberation_install
from qt_ui.companionlinkcontroller import CompanionLinkController

log = logging.getLogger(__name__)

#: How often the launcher looks for due flights, in milliseconds.
CHECK_INTERVAL_MS = 1000


class FlightLauncher(QObject):
    #: A Flight that spawned in the live mission, on the GUI thread.
    launched = Signal(object)
    #: A Flight that did not launch and why, on the GUI thread.
    launch_failed = Signal(object, str)
    #: A Flight whose launch began (it is busy now), on the GUI thread.
    launch_started = Signal(object)

    # From a worker thread: (epoch, flight, spawn id, error or None).
    _spawn_done = Signal(object, object, object, object)

    def __init__(
        self, link: CompanionLinkController, parent: Optional[QObject] = None
    ) -> None:
        super().__init__(parent)
        self._link = link
        self._status = LinkStatus.WAITING
        link.status_changed.connect(self._on_link_status)
        self._game: Optional[Game] = None
        # Bumped whenever the game changes: a worker's result of an older
        # epoch is dropped.
        self._epoch = 0
        self._in_progress: set[Flight] = set()
        self._failed: set[Flight] = set()
        # The mission the ids and names below were handed out in.
        self._mission: Optional[tuple[str, int]] = None
        self._ids = IdAllocator()
        self._sent_names: set[str] = set()
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        # (PySide6 6.4's stub lacks the signal.)
        self._timer.timeout.connect(self.check)  # type: ignore[attr-defined]
        # Queued: it is emitted on a worker thread. (PySide6 6.4's stub types
        # the connection type wrong.)
        queued = Qt.ConnectionType.QueuedConnection
        self._spawn_done.connect(self._on_spawn_done, queued)  # type: ignore[arg-type]

    def set_game(self, game: Game | None) -> None:
        """Follows the game on screen: checks every second for a dynamic game.

        The same game again (every game update) changes nothing. Another game,
        or none, forgets the flights on their way and the failed ones.
        """
        if game is self._game:
            return
        self._game = game
        self._epoch += 1
        self._in_progress.clear()
        self._failed.clear()
        self._forget_mission()
        if game is not None and game.dynamic_campaign:
            self._timer.start()
        else:
            self._timer.stop()

    def stop(self) -> None:
        """Stops checking; results of flights still on their way are dropped."""
        self.set_game(None)

    def is_busy(self, flight: Flight) -> bool:
        """Whether the flight is on its way to the live mission."""
        return flight in self._in_progress

    def launch_now(self, flight: Flight) -> None:
        """Launches the flight now, through the same path as the check.

        A flight that failed before is tried again; one on its way already,
        or launched, is left alone.
        """
        if flight in self._in_progress or flight.launched:
            return
        self._failed.discard(flight)
        self._launch(flight)

    @Slot()
    def check(self) -> None:
        """Launches every flight that is due at the mission clock's time."""
        game = self._game
        if game is None or not game.dynamic_campaign:
            return
        receiving = self._status is LinkStatus.RECEIVING
        now = LiveStateContext.clock().now()
        for flight in due_flights(
            game, now, receiving, self._in_progress, self._failed
        ):
            self._launch(flight)

    def _launch(self, flight: Flight) -> None:
        game = self._game
        if game is None or not game.dynamic_campaign:
            return
        snapshot = LiveStateContext.get()
        clock = LiveStateContext.clock()
        now = clock.now()
        model_time = clock.model_time()
        if (
            self._status is not LinkStatus.RECEIVING
            or snapshot is None
            or now is None
            or model_time is None
        ):
            log.warning(
                "Launch: the companion link is not receiving the live mission; "
                "%s not launched",
                flight,
            )
            return
        self._in_progress.add(flight)
        self.launch_started.emit(flight)

        mission = (snapshot.boot, snapshot.session)
        if mission != self._mission:
            self._forget_mission()
            self._mission = mission
        try:
            table, country, category = generate_flight_table(game, flight, now)
            request = prepare_spawn(
                table,
                country=country,
                category=category,
                snapshot=snapshot,
                ids=self._ids,
                sent_names=self._sent_names,
                model_time=model_time,
            )
        except Exception as e:
            log.warning("Launch: %s could not be generated", flight, exc_info=True)
            self._fail(flight, f"{flight} could not be generated: {_text(e)}")
            return

        log.info(
            "Launch: sending %s as %r (mission time %s, model time %.0f s)",
            flight,
            request.name,
            now.isoformat(sep=" ", timespec="seconds"),
            model_time,
        )
        worker = threading.Thread(
            target=self._send,
            args=(
                self._epoch,
                flight,
                request,
                liberation_install.companion_command_address(),
                liberation_install.companion_command_token(),
            ),
            name=f"launch {request.name}",
            daemon=True,
        )
        worker.start()

    def _send(
        self,
        epoch: int,
        flight: Flight,
        request: SpawnRequest,
        address: str,
        token: str,
    ) -> None:
        """On a worker thread: sends the spawn and follows it to `spawned`."""
        spawn_id: Optional[str] = None
        error: Optional[str] = None
        try:
            commands = CompanionCommands(address, token)
            spawn_id = commands.spawn(request)
            commands.wait_spawned(spawn_id)
        except Exception as e:
            error = _text(e)
        self._spawn_done.emit(epoch, flight, spawn_id, error)

    @Slot(object, object, object, object)
    def _on_spawn_done(
        self,
        epoch: int,
        flight: Flight,
        spawn_id: Optional[str],
        error: Optional[str],
    ) -> None:
        if epoch != self._epoch:
            log.info(
                "Launch: %s finished (%s) after its game was closed; ignored",
                flight,
                error or f"spawned as {spawn_id}",
            )
            return
        if error is not None:
            self._fail(flight, f"{flight} was not launched: {error}")
            return
        self._in_progress.discard(flight)
        flight.launched = True
        log.info("Launch: %s is in flight (spawn %s)", flight, spawn_id)
        self.launched.emit(flight)

    def _fail(self, flight: Flight, message: str) -> None:
        self._in_progress.discard(flight)
        self._failed.add(flight)
        log.warning("Launch failed: %s", message)
        self.launch_failed.emit(flight, message)

    def _forget_mission(self) -> None:
        self._mission = None
        self._ids = IdAllocator()
        self._sent_names = set()

    @Slot(object)
    def _on_link_status(self, status: LinkStatus) -> None:
        self._status = status


def _text(e: Exception) -> str:
    """The exception's message, or its type when it has none."""
    return str(e) or type(e).__name__
