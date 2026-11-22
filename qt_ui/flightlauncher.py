"""Launches a dynamic campaign's flights into the live mission.

Every second, while the companion link is receiving, the launcher asks
`due_flights` which flights of the ATO are due at the mission clock's time
and launches each: its group is generated on the GUI thread (about 0.2 s),
made fit for the live mission, then sent to DCS Companion's command port and
followed to `spawned` on a worker thread. The result comes back to the GUI
thread by a queued signal: a spawned flight is marked launched ("In Flight"),
a failed one waits for the user to press Launch again. A flight's units join
the game's name map (`game.live.name_map`) by the names they are sent with,
as the request goes out, so its losses are debriefed even before the spawn
is confirmed; a launch tried again replaces them. Nothing is launched while
the running mission is not the campaign's own
(`game.live.following_own_mission`), and a spawn confirmed while the current
mission is another's (decided foreign) leaves its flight not launched
(`launch_failed`, not failed): it launches again once the campaign's mission
is followed. A spawn confirmed while the link is merely lost or stale is
launched: the mission may still be the campaign's, and launching it again
could put a second group in it.

A spawn the companion has accepted but not yet confirmed keeps its flight
busy (not due, not editable, no Launch) while the worker follows it, up to
`CONFIRM_CAP_S`: launching it again could put a second group in the mission.
It follows through errors that may pass (no answer, 5xx, 404). Only a spawn
the companion reports `failed` or `despawned`, or a request it refuses
before accepting, fails the flight plainly; a spawn still unconfirmed at the
cap, or after an error that will not pass, fails with a message that it may
still appear in the mission.

At most one flight is launched per tick: each is generated on the GUI thread.

A flight starts as planned when it is a plane leaving an airfield: Cold, Warm,
Runway or In flight; anything else (a helicopter, a FOB, a ship, off-map)
starts in the air over its departure. A Cold or Warm flight takes the spots of
parked spare planes of its type and side first, then free spots (`plan_parking`,
on the GUI thread: it touches the theater's pydcs airport). The worker despawns
those spares before it asks for the spawn; a despawn that fails (other than a
group already gone) fails the launch before anything is spawned. Without
enough spots the flight starts in the air, with a warning in the log. The
spots and spares are claimed (`ParkingClaims`) before the worker starts and
held while the launch is on its way and a snapshot's age after, so a second
launch at the field takes others.

The group's name is one the mission does not use: not in the snapshot, not
sent into this mission by this launcher, and not held by the companion (a
spawn of its mission `waiting`, `unknown` or `spawned`, even one whose group
is gone; the companion refuses the name). The held names are asked for on the
GUI thread at each launch (`GET /v1/spawns`, a local call of milliseconds, cut
short after `HELD_TIMEOUT_S`), so they are fresh whoever spawned them: an
earlier campaign in the same mission, or this one before Retribution was
restarted. When they cannot be had, the launch goes on without them (logged
once until they can again). A group the companion holds is never a spare.

Each flight is launched at most once at a time; several flights may be on
their way at once. The ids, names and parking claims handed out belong to one
mission (the snapshot's boot and session) and start afresh with a new mission.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from typing import Optional

from dcs.terrain.terrain import ParkingSlot
from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from game import Game
from game.ato.flight import Flight
from game.ato.package import Package
from game.ato.starttype import StartType
from game.companionlink.commands import (
    CommandError,
    CompanionCommands,
    SpawnFailed,
    SpawnPending,
    SpawnRequest,
    despawn_and_spawn,
)
from game.companionlink.flightspawn import (
    IdAllocator,
    generate_flight_table,
    prepare_spawn,
    unit_names,
)
from game.companionlink.launching import due_flights
from game.companionlink.linkstatus import LinkStatus
from game.companionlink.liverecord import MissionVerdict
from game.companionlink.livestate import LiveStateContext
from game.companionlink.parking import ParkingClaims, ParkingPlan, plan_parking
from game.companionlink.snapshot import LiveSnapshot
from game.theater.controlpoint import Airfield
from qt_ui import liberation_install
from qt_ui.companionlinkcontroller import CompanionLinkController

log = logging.getLogger(__name__)

#: How often the launcher looks for due flights, in milliseconds.
CHECK_INTERVAL_MS = 1000
#: After this many seconds without `spawned` a launch is logged as late; the
#: worker keeps following it.
CONFIRM_WARN_S = 30.0
#: After this many seconds without `spawned` the launch fails (unconfirmed).
CONFIRM_CAP_S = 300.0
#: How often the worker asks for the spawn's status, in seconds.
CONFIRM_POLL_S = 0.5
#: A launch's spots are claimed while it is on its way, then until this long
#: after the mission clock's now as it ends (one snapshot period): the clock
#: between snapshots is a wall-clock estimate, and a snapshot taken before the
#: spawn landed must not free them.
CLAIM_AHEAD_S = 60.0
#: How long a launch waits for the companion's held names, in seconds; the
#: GUI thread waits on it.
HELD_TIMEOUT_S = 2.0


def flight_locked(flight: Flight, launcher: Optional[FlightLauncher] = None) -> bool:
    """Whether a dynamic campaign's flight is in flight or on its way there.

    Such a flight can be neither edited nor deleted. `launcher` defaults to
    the window's launcher (`FlightLauncher.current()`).
    """
    if not flight.coalition.game.dynamic_campaign:
        return False
    if flight.launched:
        return True
    launcher = launcher or FlightLauncher.current()
    return launcher is not None and launcher.is_busy(flight)


def package_locked(package: Package, launcher: Optional[FlightLauncher] = None) -> bool:
    """Whether a package holds a flight that is in flight or on its way there.

    Such a package cannot be deleted.
    """
    return any(flight_locked(f, launcher) for f in package.flights)


class FlightLauncher(QObject):
    #: A Flight that spawned in the live mission, on the GUI thread.
    launched = Signal(object)
    #: A Flight that did not launch and why, on the GUI thread; also one whose
    #: spawn was confirmed in another mission (it launches again).
    launch_failed = Signal(object, str)
    #: A Flight whose launch began (it is busy now), on the GUI thread.
    launch_started = Signal(object)
    #: Whether flights can be launched now (the link is receiving).
    receiving_changed = Signal(bool)

    #: The launcher of the main window, for UI that cannot be handed one.
    _current: Optional[FlightLauncher] = None

    # From a worker thread: (epoch, flight, spawn id, error or None, whether
    # the companion holds the spawn though it was not confirmed).
    _spawn_done = Signal(object, object, object, object, object)

    def __init__(
        self, link: CompanionLinkController, parent: Optional[QObject] = None
    ) -> None:
        super().__init__(parent)
        FlightLauncher._current = self
        self._link = link
        self.confirm_warn_s = CONFIRM_WARN_S
        self.confirm_cap_s = CONFIRM_CAP_S
        self.confirm_poll_s = CONFIRM_POLL_S
        self._status = LinkStatus.WAITING
        link.status_changed.connect(self._on_link_status)
        self._game: Optional[Game] = None
        # Bumped whenever the game changes: a worker's result of an older
        # epoch is dropped.
        self._epoch = 0
        self._in_progress: set[Flight] = set()
        self._failed: set[Flight] = set()
        # The group name each flight on its way was sent as.
        self._sent_as: dict[Flight, str] = {}
        # The mission the ids and names below were handed out in.
        self._mission: Optional[tuple[str, int]] = None
        self._ids = IdAllocator()
        self._sent_names: set[str] = set()
        self._parking = ParkingClaims()
        # Whether the held names could not be had at the last ask (logged).
        self._held_failed = False
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        # (PySide6 6.4's stub lacks the signal.)
        self._timer.timeout.connect(self.check)  # type: ignore[attr-defined]
        # Queued: it is emitted on a worker thread. (PySide6 6.4's stub types
        # the connection type wrong.)
        queued = Qt.ConnectionType.QueuedConnection
        self._spawn_done.connect(self._on_spawn_done, queued)  # type: ignore[arg-type]

    @classmethod
    def current(cls) -> Optional[FlightLauncher]:
        """The most recently made launcher (the main window's), if any."""
        return cls._current

    @property
    def receiving(self) -> bool:
        """Whether the link is receiving the live mission, so a flight can be
        launched."""
        return self._status is LinkStatus.RECEIVING

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
        self._sent_as.clear()
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

    @property
    def launching(self) -> bool:
        """Whether any flight is on its way to the live mission."""
        return bool(self._in_progress)

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
        """Launches the first flight that is due at the mission clock's time."""
        game = self._game
        if game is None or not game.dynamic_campaign:
            return
        receiving = self._status is LinkStatus.RECEIVING
        now = LiveStateContext.clock().now()
        due = due_flights(game, now, receiving, self._in_progress, self._failed)
        # One a tick: each is generated on the GUI thread, and many may be due
        # at once (a reopened save). The others are due again next tick.
        if due:
            self._launch(due[0])

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
        if not game.live.following_own_mission:
            log.warning(
                "Launch: the running mission is not this campaign's; "
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
        held = self._held_names()
        try:
            start_type, plan = self._plan_start(game, flight, snapshot, held)
            table, country, category = generate_flight_table(
                game,
                flight,
                now,
                start_type=start_type,
                parking=plan.slots if plan is not None else None,
            )
            request = prepare_spawn(
                table,
                country=country,
                category=category,
                snapshot=snapshot,
                ids=self._ids,
                sent_names=self._sent_names,
                model_time=model_time,
                held_names=held,
            )
        except Exception as e:
            log.warning("Launch: %s could not be generated", flight, exc_info=True)
            self._fail(flight, f"{flight} could not be generated: {_text(e)}")
            return

        despawn: list[str] = []
        if plan is not None:
            self._parking.claim(plan, flight)
            despawn = list(plan.despawn)
            log.info(
                "Launch: %s starts %s at %s on %s; despawning first: %s",
                flight,
                start_type.value,
                flight.departure.name,
                ", ".join(_slot_name(s) for s in plan.slots),
                ", ".join(repr(n) for n in despawn) or "none",
            )
        log.info(
            "Launch: sending %s as %r (mission time %s, model time %.0f s)",
            flight,
            request.name,
            now.isoformat(sep=" ", timespec="seconds"),
            model_time,
        )
        self._sent_as[flight] = request.name
        game.live.name_map.add_launched(flight, unit_names(request))
        worker = threading.Thread(
            target=self._send,
            args=(
                self._epoch,
                flight,
                despawn,
                request,
                liberation_install.companion_command_address(),
                liberation_install.companion_command_token(),
                self.confirm_warn_s,
                self.confirm_cap_s,
                self.confirm_poll_s,
            ),
            name=f"launch {request.name}",
            daemon=True,
        )
        worker.start()

    def _send(
        self,
        epoch: int,
        flight: Flight,
        despawn: Sequence[str],
        request: SpawnRequest,
        address: str,
        token: str,
        warn_s: float,
        cap_s: float,
        poll_s: float,
    ) -> None:
        """On a worker thread: despawns the parked spares on the flight's
        spots (`despawn`, in order), sends the spawn and follows it to
        `spawned`.

        A despawn that fails (but for a group gone already) fails the launch
        before the spawn is sent: a plane may still stand on the spot.

        Once the companion has accepted the spawn it holds it, so the flight
        stays busy while the worker follows it, up to `cap_s` in all: through
        a spawn still waiting (logged after `warn_s`) and through errors that
        may pass (no answer, 5xx, 404, 408, 429). Only a `failed` or
        `despawned` spawn fails it plainly; whatever else ends it unconfirmed
        says the spawn may still appear.
        """
        commands = CompanionCommands(address, token)
        try:
            spawn_id = despawn_and_spawn(commands, despawn, request)
        except Exception as e:
            self._spawn_done.emit(epoch, flight, None, _text(e), False)
            return

        asked = time.monotonic()
        warned = False
        last = "waiting"
        seen: set[str] = set()
        while True:
            elapsed = time.monotonic() - asked
            left = cap_s - elapsed
            if left <= 0:
                error = (
                    f"spawn {spawn_id} was not confirmed within {cap_s:g} s "
                    f"(last: {last}); it may still appear in the mission"
                )
                self._spawn_done.emit(epoch, flight, spawn_id, error, True)
                return
            if not warned and elapsed >= warn_s:
                warned = True
                log.warning(
                    "Launch: %r (spawn %s) not spawned after %g s (last: %s); "
                    "following it up to %g s",
                    request.name,
                    spawn_id,
                    warn_s,
                    last,
                    cap_s,
                )
            step = left if warned else min(left, warn_s - elapsed)
            try:
                commands.wait_spawned(spawn_id, limit=max(step, 0.0), poll=poll_s)
            except SpawnFailed as e:
                self._spawn_done.emit(epoch, flight, spawn_id, _text(e), False)
                return
            except SpawnPending as e:
                last = f"still {e.last_status}"
                continue
            except CommandError as e:
                if not _transient(e):
                    error = (
                        f"spawn {spawn_id} was not confirmed ({_text(e)}); "
                        "it may still appear in the mission"
                    )
                    self._spawn_done.emit(epoch, flight, spawn_id, error, True)
                    return
                last = _text(e)
                if last not in seen:
                    seen.add(last)
                    log.warning(
                        "Launch: following spawn %s of %r: %s; trying again",
                        spawn_id,
                        request.name,
                        last,
                    )
                time.sleep(min(poll_s, max(cap_s - (time.monotonic() - asked), 0)))
                continue
            except Exception as e:
                error = (
                    f"spawn {spawn_id} was not confirmed ({_text(e)}); "
                    "it may still appear in the mission"
                )
                self._spawn_done.emit(epoch, flight, spawn_id, error, True)
                return
            self._spawn_done.emit(epoch, flight, spawn_id, None, False)
            return

    @Slot(object, object, object, object, object)
    def _on_spawn_done(
        self,
        epoch: int,
        flight: Flight,
        spawn_id: Optional[str],
        error: Optional[str],
        unconfirmed: bool,
    ) -> None:
        if epoch != self._epoch:
            log.info(
                "Launch: %s finished (%s) after its game was closed; ignored",
                flight,
                error or f"spawned as {spawn_id}",
            )
            return
        if error is not None:
            if unconfirmed:
                self._fail(flight, f"{flight} is not confirmed in flight: {error}")
            else:
                self._fail(flight, f"{flight} was not launched: {error}")
            return
        game = self._game
        if game is not None and game.live.current_verdict is MissionVerdict.FOREIGN:
            # Not the campaign's mission: the flight launches again when it
            # is. Its names, mapped at send, are replaced by that launch. Not
            # failed, so it is due again; the signal ends the launch on screen.
            self._end_launch(flight)
            self._sent_as.pop(flight, None)
            message = (
                f"{flight} spawned after the campaign's mission stopped being "
                "followed; it launches again"
            )
            log.warning("Launch: %s", message)
            self.launch_failed.emit(flight, message)
            return
        self._end_launch(flight)
        flight.launched = True
        flight.live_group_name = self._sent_as.pop(flight, None)
        log.info("Launch: %s is in flight (spawn %s)", flight, spawn_id)
        self.launched.emit(flight)

    def _fail(self, flight: Flight, message: str) -> None:
        self._end_launch(flight)
        self._sent_as.pop(flight, None)
        self._failed.add(flight)
        log.warning("Launch failed: %s", message)
        self.launch_failed.emit(flight, message)

    def _end_launch(self, flight: Flight) -> None:
        """The flight is no longer on its way: its parking claims hold on a
        snapshot's age from now."""
        self._in_progress.discard(flight)
        model_time = LiveStateContext.clock().model_time()
        if model_time is None:
            # No mission clock (a new mission comes): the claims stay held
            # until the mission is forgotten.
            return
        self._parking.release(flight, model_time + CLAIM_AHEAD_S)

    def _forget_mission(self) -> None:
        self._mission = None
        self._ids = IdAllocator()
        self._sent_names = set()
        self._parking = ParkingClaims()

    def _held_names(self) -> frozenset[str]:
        """The names the companion holds in its mission, or none when it
        cannot be asked: the launch goes on, and may be refused (`409`)."""
        commands = CompanionCommands(
            liberation_install.companion_command_address(),
            liberation_install.companion_command_token(),
            timeout=HELD_TIMEOUT_S,
        )
        try:
            held = commands.held_names()
        except Exception as e:
            if not self._held_failed:
                log.warning(
                    "Launch: the companion's held group names cannot be had "
                    "(%s); choosing names from the snapshot only",
                    _text(e),
                )
            self._held_failed = True
            return frozenset()
        if self._held_failed:
            log.info("Launch: the companion's held group names can be had again")
        self._held_failed = False
        return held

    def _plan_start(
        self, game: Game, flight: Flight, snapshot: LiveSnapshot, held: frozenset[str]
    ) -> tuple[StartType, Optional[ParkingPlan]]:
        """The start type the flight is generated with, and its spots.

        A plane leaving an airfield starts as planned, anything else in the
        air. A Cold or Warm start gets its spots from the snapshot, or starts
        in the air if there are too few. Not claimed yet.
        """
        departure = flight.departure
        if flight.unit_type.helicopter or not isinstance(departure, Airfield):
            return StartType.IN_FLIGHT, None
        start_type = flight.start_type
        if start_type not in (StartType.COLD, StartType.WARM):
            return start_type, None
        plan = plan_parking(
            departure.airport,
            flight.unit_type.dcs_unit_type,
            flight.count,
            flight.coalition.coalition_id,
            snapshot,
            self._not_spares(game) | held,
            self._parking,
        )
        if plan is None:
            log.warning(
                "%s: no parking for %d at %s; starting in the air",
                flight,
                flight.count,
                departure.name,
            )
            return StartType.IN_FLIGHT, None
        return start_type, plan

    def _not_spares(self, game: Game) -> set[str]:
        """The groups never despawned for a launch: those sent into this
        mission and every flight's live group, of both ATOs. (The names the
        companion holds are added by the caller.)"""
        names = set(self._sent_names)
        for ato in (game.blue.ato, game.red.ato):
            for package in ato.packages:
                for f in package.flights:
                    if f.live_group_name is not None:
                        names.add(f.live_group_name)
        return names

    @Slot(object)
    def _on_link_status(self, status: LinkStatus) -> None:
        was = self.receiving
        self._status = status
        if self.receiving != was:
            self.receiving_changed.emit(self.receiving)


def _transient(e: CommandError) -> bool:
    """Whether following a spawn may get past the error: no answer (connect
    error, timeout), a server error, or a record the companion cannot give
    now."""
    return e.status is None or e.status >= 500 or e.status in (404, 408, 429)


def _slot_name(slot: ParkingSlot) -> str:
    """The slot's name in the airfield's parking list, or its index."""
    return slot.slot_name or f"#{slot.crossroad_idx}"


def _text(e: Exception) -> str:
    """The exception's message, or its type when it has none."""
    return str(e) or type(e).__name__
