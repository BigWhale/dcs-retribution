"""A dynamic campaign's Begin and Continue: the mission written, loaded on the
server through DCS Companion, and waited for.

Begin (turn 0) passes the turn and writes the campaign's starting mission.
Continue (after) writes the campaign's mission again: from the tick with
slice 2's restore (`write_restore`), or, without a tick (the campaign's
mission never ran), from the game as it stands, as Begin writes it but without
the turn pass. Each asks first: a dialog says the running mission ends.

Every mission is written under its own dated name in DCS's `Missions/`
(`persistency.dynamic_mission_path`), through a `.tmp` and a rename, under a
new marker; the game's earlier missions are retired, so none of them is
followed again. A failed write changes nothing but Begin's turn pass: the
marker, the verdicts, the mission file and the time stay as they were.

Once written, the game no longer awaits a pick-up (`end_pickup`), the link
runs, and the companion is asked to load the file (`POST /v1/mission/load`,
on a worker thread, cut short after `LOAD_TIMEOUT_S`; its answer comes back
queued). The companion's `202` names the mission it replaces; the wait ends at
the first mission the link sees that is not that one (the campaign's new
mission, followed by its marker, or another one), or, with none within
`LOAD_WAIT_S` of the request, says the file was not loaded (as a refused
request does) until the campaign's mission is followed after all. The state
shows in the link's status (`set_loading`); it is not saved, and another game,
or a new Begin or Continue, forgets it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, cast

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QMessageBox, QWidget

from game import Game, persistency
from game.companionlink.commands import CommandError, CompanionCommands
from game.companionlink.liverecord import LiveTick
from game.companionlink.liveunitmap import LiveUnitMap
from game.companionlink.restore import restore_start, write_restore
from game.companionlink.snapshot import LiveSnapshot
from game.profiling import logged_duration
from qt_ui import liberation_install
from qt_ui.companionlinkcontroller import CompanionLinkController
from qt_ui.models import GameModel
from qt_ui.simcontroller import SimController
from qt_ui.windows.GameUpdateSignal import GameUpdateSignal

log = logging.getLogger(__name__)

#: How long the server has to load a mission, from the request, in seconds.
LOAD_WAIT_S = 180.0
#: How long the load request may take, in seconds.
LOAD_TIMEOUT_S = 10.0
#: How often the wait looks at its clock, in milliseconds.
WAIT_CHECK_MS = 1_000

#: The end of every dialog that loads a mission.
_DROPPED = "The mission running on the server ends; players go back to slot selection."


class CampaignMission(QObject):
    #: Begin or Continue changed what is loading or failed to load.
    changed = Signal()

    # From the worker: (the request's epoch, the file, the mission replaced
    # or the CommandError).
    _load_answered = Signal(int, str, object)

    def __init__(
        self,
        game_model: GameModel,
        sim_controller: SimController,
        companion_link: CompanionLinkController,
        parent: Optional[QObject] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(parent)
        self.game_model = game_model
        self.sim_controller = sim_controller
        self.companion_link = companion_link
        self._clock = clock
        self._game: Optional[Game] = None
        # The file asked to load, until its mission comes or the wait ends.
        self._loading: Optional[str] = None
        # The file whose load was refused or not seen within the wait.
        self._load_failed: Optional[str] = None
        # The mission the load replaces, as the companion's 202 named it.
        self._replaced: Optional[tuple[str, int]] = None
        # When the wait ends without a new mission (the clock's seconds).
        self._deadline = 0.0
        # Counts the requests; an answer of an earlier one is dropped.
        self._epoch = 0
        self._wait_timer = QTimer(self)
        # (PySide6 6.4's stub lacks the signal.)
        self._wait_timer.timeout.connect(self._check_wait)  # type: ignore[attr-defined]
        # Queued: emitted on the worker's thread. (PySide6 6.4's stub types
        # the connection type wrong.)
        queued = Qt.ConnectionType.QueuedConnection
        self._load_answered.connect(self._on_load_answered, queued)  # type: ignore[arg-type]
        companion_link.snapshot_received.connect(self._on_snapshot)
        companion_link.verdict_changed.connect(self._on_verdict)

    @property
    def loading(self) -> Optional[str]:
        """The mission file whose load is waited for; None when none is."""
        return self._loading

    @property
    def load_failed(self) -> Optional[str]:
        """The mission file that was not loaded; None when none failed."""
        return self._load_failed

    def set_game(self, game: Optional[Game]) -> None:
        """Follows the game on screen; another game forgets the load."""
        if game is self._game:
            return
        self._game = game
        self._set_load(None, None)

    def begin(self) -> None:
        """Begins a dynamic campaign at turn 0: asks, passes the turn, writes
        the starting mission and has it loaded."""
        game = self.game_model.game
        if game is None or not game.dynamic_campaign or game.turn != 0:
            return
        if not self._ask(
            "Begin the campaign?",
            "Retribution writes the campaign's starting mission and DCS "
            f"Companion loads it on the server. {_DROPPED}",
        ):
            return
        # The mission written is followed by its marker, not picked up.
        game.live.end_pickup()
        path = persistency.dynamic_mission_path(datetime.now())
        with logged_duration("Beginning dynamic campaign"):
            game.pass_turn(no_action=True)
            # Recreates the sim for the new turn, so it comes before
            # generation.
            GameUpdateSignal.get_instance().updateGame(game)
            written = self._write(game, path, self._write_from_game)
            if written:
                log.info("Dynamic campaign begun, mission written to %s", path)
                persistency.autosave(game)
        self.companion_link.start(game)
        if written:
            self._load(path.name)

    def continue_(self) -> None:
        """Writes the campaign's mission again (from the tick, else as it
        stands) after asking, and has it loaded; not while a load is waited
        for."""
        game = self.game_model.game
        if game is None or not game.dynamic_campaign or game.turn == 0:
            return
        if self._loading is not None:
            return
        tick = game.live.tick
        if not self._confirm_continue(game, tick):
            return
        path = persistency.dynamic_mission_path(datetime.now())
        with logged_duration("Continuing dynamic campaign"):
            if tick is not None:
                written = self._write(game, path, write_restore)
            else:
                written = self._write(game, path, self._write_from_game)
            if not written:
                return
            # The mission written is followed by its marker, not picked up.
            game.live.end_pickup()
            GameUpdateSignal.get_instance().updateGame(game)
            persistency.autosave(game)
            # The mission replaced is no longer followed, at once.
            self.companion_link.recheck()
        self.companion_link.start(game)
        self._load(path.name)

    def _confirm_continue(self, game: Game, tick: Optional[LiveTick]) -> bool:
        if tick is not None and game.live.following_own_mission:
            return self._ask(
                "Restore the campaign's mission?",
                "The campaign's mission is running. The restored mission starts "
                f"at {restore_start(tick):%H:%M}, minute {tick.minute} of the "
                "running one. From now on only the restored mission is followed; "
                "nothing that happens in the running one is applied. DCS "
                "Companion loads it on the server; players go back to slot "
                "selection.",
            )
        when = (
            "as it stands now"
            if tick is None
            else f"as it stood at {restore_start(tick):%Y-%m-%d %H:%M}"
        )
        return self._ask(
            "Continue the campaign?",
            f"Retribution writes the campaign's mission {when} and DCS Companion "
            f"loads it on the server. {_DROPPED}",
        )

    def _ask(self, title: str, text: str) -> bool:
        result = QMessageBox.question(
            self._dialog_parent(),
            title,
            text,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return result == QMessageBox.StandardButton.Ok

    def _dialog_parent(self) -> QWidget:
        """The window the dialogs belong to; none without one (Qt takes None,
        PySide6 6.4's stub does not)."""
        parent = self.parent()
        return parent if isinstance(parent, QWidget) else cast(QWidget, None)

    def _write(
        self, game: Game, path: Path, write: Callable[[Game, Path], object]
    ) -> bool:
        """Writes the mission with `write`; on success the game records the
        file's name. On a failure, which leaves the game as it was, says so;
        whether it was written."""
        try:
            write(game, path)
        except Exception as ex:
            log.exception("Dynamic campaign: writing %s failed", path)
            QMessageBox.warning(
                self._dialog_parent(),
                "Mission not written",
                f"{path.name} was not written: {ex}",
                QMessageBox.StandardButton.Ok,
            )
            return False
        game.live.mission_file = path.name
        return True

    def _write_from_game(self, game: Game, path: Path) -> None:
        """Writes the mission from the game as it stands, the ATO's flights
        with it, under a new marker; then keeps its name map and retires the
        campaign's earlier missions. A failure puts the marker back and
        raises."""
        record = game.live
        marker, written_map = record.marker, record.written_map
        # Written beside `path` and moved onto it whole.
        temporary = path.with_name(path.name + ".tmp")
        try:
            record.new_marker()
            # A sim of the game as it stands: the flights planned since the
            # last turn are in it.
            self.sim_controller.recreate_game_loop(game)
            self.sim_controller.generate_miz(temporary)
            os.replace(temporary, path)
        except BaseException:
            record.marker, record.written_map = marker, written_map
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise
        # Its losses are debriefed against the names it was written with,
        # kept with the save.
        game_loop = self.sim_controller.game_loop
        unit_map = game_loop.sim.unit_map if game_loop is not None else None
        if unit_map is None:
            log.error(
                "Dynamic campaign: %s was written but its name map is missing; "
                "live losses will not apply",
                path,
            )
            QMessageBox.warning(
                self._dialog_parent(),
                "Live losses will not apply",
                (
                    f"The campaign's mission ({path.name}) was written, but its "
                    "name map is missing, so live losses will not apply to the "
                    "campaign.<br /><br />Start a new dynamic campaign."
                ),
                QMessageBox.StandardButton.Ok,
            )
        else:
            record.written(LiveUnitMap.from_generation(unit_map, game))
        record.retire()

    def _load(self, file: str) -> None:
        """Asks the companion to load `file`, on a worker thread; the wait
        starts now."""
        self._epoch += 1
        epoch = self._epoch
        self._replaced = None
        self._deadline = self._clock() + LOAD_WAIT_S
        self._set_load(file, None)
        self._wait_timer.start(WAIT_CHECK_MS)
        commands = CompanionCommands(
            liberation_install.companion_command_address(),
            liberation_install.companion_command_token(),
            timeout=LOAD_TIMEOUT_S,
        )
        log.info("Mission load: asked %s", file)

        def ask() -> None:
            answer: object
            try:
                answer = commands.load_mission(file)
            except CommandError as e:
                answer = e
            self._load_answered.emit(epoch, file, answer)

        threading.Thread(target=ask, name="mission load", daemon=True).start()

    @Slot(int, str, object)
    def _on_load_answered(self, epoch: int, file: str, answer: object) -> None:
        if epoch != self._epoch or self._loading != file:
            return
        if isinstance(answer, CommandError):
            log.warning("Mission load: %s failed: %s", file, answer)
            self._set_load(None, file)
            QMessageBox.warning(
                self._dialog_parent(),
                "Mission not loaded",
                f"{file} was written but DCS Companion did not load it: {answer}",
                QMessageBox.StandardButton.Ok,
            )
            return
        assert isinstance(answer, tuple)
        self._replaced = answer
        # The new mission may have been seen before the answer came.
        game = self.game_model.game
        if game is not None and game.live.current is not None:
            self._mission_seen(game.live.current)

    @Slot(object)
    def _on_snapshot(self, snapshot: LiveSnapshot) -> None:
        """A snapshot of the campaign's own mission, followed."""
        if self._loading is None and self._load_failed is not None:
            # Loaded after all: nothing failed.
            self._set_load(None, None)
            return
        self._mission_seen((snapshot.boot, snapshot.session))

    @Slot(object)
    def _on_verdict(self, _verdict: object) -> None:
        """A mission newly current, whichever it is."""
        game = self.game_model.game
        if game is not None and game.live.current is not None:
            self._mission_seen(game.live.current)

    def _mission_seen(self, mission: tuple[str, int]) -> None:
        """The link sees `mission`: one other than the one replaced ends the
        wait."""
        if self._loading is None or self._replaced is None:
            return
        if mission == self._replaced:
            return
        self._set_load(None, None)

    @Slot()
    def _check_wait(self) -> None:
        file = self._loading
        if file is None or self._clock() < self._deadline:
            return
        log.warning("Mission load: %s not loaded within %.0f s", file, LOAD_WAIT_S)
        self._set_load(None, file)

    def _set_load(self, loading: Optional[str], failed: Optional[str]) -> None:
        """What is loading and what failed to; with nothing loading, the
        wait and any answer still to come are done with."""
        if loading is None:
            self._epoch += 1
            self._replaced = None
            self._wait_timer.stop()
        if (loading, failed) == (self._loading, self._load_failed):
            return
        self._loading = loading
        self._load_failed = failed
        self.companion_link.set_loading(loading, failed)
        self.changed.emit()
