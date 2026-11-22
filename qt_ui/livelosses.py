"""Applies a dynamic campaign's live losses on the GUI thread.

The link's controller hands over the companion's event log a page at a time
(`events_received`). The game's live record holds the entries until their
mission is decided and releases only the campaign's own (`game.live.ready`).
They are applied at most `CHUNK` at a time, each chunk a batch of its own
(`apply_entries`, then `settle`), and the rest on the next turn of the Qt
event loop, so a catch-up of thousands of entries never stalls the window.
A new verdict may release held entries, so it drains too. The batch that
gives the game its first cursor saves the game (autosave), so a reload does
not start the cursor afresh past the mission's entries.

A finished flight (every unit dead or landed) and a stranded one (not
launched, its departure captured) leave the ATO through its Qt models, so
the lists follow. A flight whose package is open in the package dialog is
removed when that dialog closes; until then it stays where it is. The map
gets the batch's events once per batch.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtWidgets import QDialog

from game import Game, persistency
from game.ato.flight import Flight
from game.companionlink.eventlog import EventBatch
from game.companionlink.livelosses import LiveBatch, apply_entries, settle
from game.server import EventStream
from game.sim import GameUpdateEvents
from qt_ui.companionlinkcontroller import CompanionLinkController
from qt_ui.dialogs import Dialog
from qt_ui.models import GameModel, PackageModel

log = logging.getLogger(__name__)

#: At most this many entries are applied at a time.
CHUNK = 500
#: A batch slower than this many milliseconds is also logged as a warning.
SLOW_MS = 250


class LiveLosses(QObject):
    def __init__(
        self, link: CompanionLinkController, parent: Optional[QObject] = None
    ) -> None:
        super().__init__(parent)
        self._game_model: Optional[GameModel] = None
        self._game: Optional[Game] = None
        # Flights waiting for their package dialog to close, by dialog: the
        # flight, and whether it was stranded (not finished).
        self._deferred: dict[QDialog, list[tuple[Flight, bool]]] = {}
        link.events_received.connect(self._on_events)
        link.verdict_changed.connect(self._on_verdict)

    def set_game(self, game_model: GameModel) -> None:
        """Follows the game on screen. Another game forgets the flights
        waiting for a dialog."""
        self._game_model = game_model
        if game_model.game is not self._game:
            self._game = game_model.game
            self._deferred.clear()

    def _dynamic_game(self) -> Optional[Game]:
        game = self._game
        if game is None or not game.dynamic_campaign:
            return None
        return game

    @Slot(object)
    def _on_events(self, batch: EventBatch) -> None:
        game = self._dynamic_game()
        if game is None:
            return
        had_cursor = game.live.cursor is not None
        game.live.receive(batch)
        self._drain()
        if not had_cursor and game.live.cursor is not None:
            persistency.autosave(game)

    @Slot(object)
    def _on_verdict(self, _verdict: object) -> None:
        self._drain()

    @Slot()
    def _drain(self) -> None:
        """Applies the next chunk of ready entries; another follows on the next
        turn of the event loop until none is ready."""
        game = self._dynamic_game()
        if game is None:
            return
        entries = game.live.ready(CHUNK)
        if not entries:
            return
        first, last = entries[0].seq, entries[-1].seq
        started = time.perf_counter()
        batch: Optional[LiveBatch] = None
        try:
            batch = apply_entries(game, entries)
            self._remove_flights(game, batch)
        except Exception:
            # One bad entry must not wedge the campaign: the chunk is skipped,
            # visibly, and the cursor moves on.
            log.exception(
                "Live losses: applying entries %d..%d failed; skipped", first, last
            )
            failed = True
        else:
            failed = False
        try:
            if batch is not None:
                settle(game, batch)
                EventStream.put_nowait(batch.events)
        finally:
            game.live.applied(last)
            # Whatever is left (a full chunk, or entries behind one to drop)
            # waits for the next turn of the event loop.
            QTimer.singleShot(0, self._drain)
        if failed or batch is None:
            return
        ms = (time.perf_counter() - started) * 1000
        line = (
            f"Live losses: {batch.entries} entries, {len(batch.dead)} dead, "
            f"{len(batch.captured)} captured, {len(batch.finished)} flights "
            f"finished, {ms:.0f} ms"
        )
        log.info(line)
        if ms > SLOW_MS:
            log.warning(line)

    def _remove_flights(self, game: Game, batch: LiveBatch) -> None:
        """The batch's finished and stranded flights leave the ATO."""
        for flight in batch.finished:
            self._remove(flight, stranded=False, events=batch.events)
        for flight in batch.stranded:
            game.message(
                f"{flight} not launched",
                f"{flight.departure.name} was captured; the flight is removed from "
                "its package.",
            )
            self._remove(flight, stranded=True, events=batch.events)

    def _remove(self, flight: Flight, stranded: bool, events: GameUpdateEvents) -> None:
        """Removes the flight through the ATO's models, or when the package
        dialog showing its package closes."""
        dialog = Dialog.edit_package_dialog
        if (
            dialog is not None
            and dialog.isVisible()
            and dialog.package_model.package is flight.package
        ):
            if dialog not in self._deferred:
                self._deferred[dialog] = []
                # (PySide6 6.4's stub lacks the signal.)
                dialog.finished.connect(  # type: ignore[attr-defined]
                    lambda _result, d=dialog: self._remove_deferred(d)
                )
            waiting = self._deferred[dialog]
            if not any(f is flight for f, _ in waiting):
                waiting.append((flight, stranded))
            return
        self._delete(flight, events)

    def _remove_deferred(self, dialog: QDialog) -> None:
        """The package dialog closed: its flights waiting leave the ATO; a
        stranded one launched meanwhile flies on."""
        deferred = self._deferred.pop(dialog, [])
        game = self._dynamic_game()
        if game is None:
            return
        events = GameUpdateEvents()
        removed = []
        for flight, stranded in deferred:
            if stranded and flight.launched:
                continue
            if any(f is flight for f in flight.package.flights):
                self._delete(flight, events)
                removed.append(flight)
        if not removed:
            return
        settle(
            game,
            LiveBatch(
                entries=0,
                dead=[],
                captured=[],
                finished=[],
                stranded=[],
                squadrons={f.squadron for f in removed},
                threat_changed=False,
                events=events,
            ),
        )
        EventStream.put_nowait(events)

    def _delete(self, flight: Flight, events: GameUpdateEvents) -> None:
        """Removes the flight from its package, and the package from its ATO
        when it is left empty, through the Qt models."""
        game_model = self._game_model
        assert game_model is not None
        package = flight.package
        ato_model = game_model.ato_model_for(flight.blue)
        package_model: PackageModel = ato_model.package_models.acquire(package)
        package_model.delete_flight(flight)
        events.delete_flight(flight)
        if not package.flights and package in ato_model.ato.packages:
            ato_model.cancel_or_abort_package(package)
