"""A dynamic campaign's GUI side of live losses, against a fake companion.

The main window's link controller and `LiveLosses` run on an offscreen Qt
application, the link reads a fake companion over HTTP (`FakeCompanion`), and
the game is Begin Campaign's, generated headless. What is checked is what
the window shows or sends: the companion status, the live state and the
mission clock, the map's events, the ATO, the event log cursor and the log.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

from game import persistency  # noqa: E402
from game.ato.flight import Flight  # noqa: E402
from game.companionlink.commands import SpawnsView  # noqa: E402
from game.companionlink.eventlog import EventBatch  # noqa: E402
from game.companionlink.livestate import LiveStateContext  # noqa: E402
from game.companionlink.liverecord import (  # noqa: E402
    MARKER_PREFIX,
    MissionVerdict,
    SavedAt,
)
from game.companionlink.liveunitmap import LiveUnitMap  # noqa: E402
from game.companionlink.snapshot import parse_snapshot  # noqa: E402
from game.game import Game  # noqa: E402
from game.server import EventStream  # noqa: E402
from game.sim import GameUpdateEvents  # noqa: E402
from game.theater import Player  # noqa: E402
from qt_ui import (  # noqa: E402
    companionlinkcontroller,
    dynamicautosave,
    flightlauncher,
    liberation_install,
    liberation_theme,
    livelosses,
    uiconstants,
)
from qt_ui.companionlinkcontroller import CompanionLinkController  # noqa: E402
from qt_ui.dialogs import Dialog  # noqa: E402
from qt_ui.dynamicautosave import DynamicAutosave  # noqa: E402
from qt_ui.flightlauncher import FlightLauncher  # noqa: E402
from qt_ui.livelosses import LiveLosses  # noqa: E402
from qt_ui.models import GameModel, PackageModel  # noqa: E402
from qt_ui.simcontroller import SimController  # noqa: E402
from qt_ui.uiflags import UiFlags  # noqa: E402
from qt_ui.widgets.QTopPanel import QTopPanel  # noqa: E402
from qt_ui.windows.GameUpdateSignal import GameUpdateSignal  # noqa: E402
from qt_ui.windows.QLiberationWindow import QLiberationWindow  # noqa: E402
import qt_ui.windows.QLiberationWindow as liberation_window  # noqa: E402
from tests.conftest import (  # noqa: E402
    COMMAND_TOKEN,
    TOKEN,
    FakeCommands,
    FakeCompanion,
    Item,
    entry,
    event,
    generate_starting_mission,
    new_game,
    plan_by_hand,
    save_and_load,
    wait_until,
)

CAMPAIGN = Path("resources/campaigns/golan_heights_lite.yaml")
DEADLINE = 5.0
BATCH_LINE = re.compile(r"Live losses: \d+ entries, ")
AUTOSAVE_LINE = re.compile(r"Autosave: (.+) in \d+ ms")
#: The status while another mission runs.
FOLLOWED_NOT = (
    "Companion: another mission is running; Continue to load the campaign's mission"
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    existing = QApplication.instance()
    return existing if isinstance(existing, QApplication) else QApplication([])


@pytest.fixture
def game(saved_games: Path, tmp_path: Path) -> Game:
    """Begin Campaign's game: its mission written with a marker."""
    game = new_game(dynamic=True, squadrons_start_full=True, campaign_file=CAMPAIGN)
    game.pass_turn(no_action=True)
    game.live.new_marker()
    sim = generate_starting_mission(game, tmp_path)
    assert sim.unit_map is not None
    game.live.written(LiveUnitMap.from_generation(sim.unit_map, game))
    return game


class Window:
    """The main window's link controller and live losses for a game; the
    map's events it sends are kept in `map_events` (the `window` fixture
    records `EventStream.put_nowait`)."""

    def __init__(self, app: QApplication, game: Game) -> None:
        self.app = app
        self.game = game
        self.game_model = GameModel(game, SimController(game))
        self.link = CompanionLinkController()
        self.losses = LiveLosses(self.link)
        self.losses.set_game(self.game_model)
        self.map_events: list[GameUpdateEvents] = []
        self.panel: Optional[QTopPanel] = None

    def start(self) -> None:
        self.link.set_game(self.game)

    def top_panel(self) -> QTopPanel:
        """The main window's top panel, shown the game as a loaded save is;
        each game update reaches the window's side after the panel, as in the
        main window (`set_game`)."""
        liberation_theme.set_theme_index(liberation_theme.DEFAULT_THEME_INDEX)
        uiconstants.load_icons()  # the panel's buttons
        signal = GameUpdateSignal.get_instance() or GameUpdateSignal()
        self.panel = QTopPanel(
            self.game_model,
            self.game_model.sim_controller,
            UiFlags(dev_ui_webserver=False, show_sim_speed_controls=False),
            self.link,
        )
        signal.gameupdated.connect(self.set_game)
        signal.updateGame(self.game)
        return self.panel

    def set_game(self, game: Optional[Game]) -> None:
        """The main window on a game update: the sim, the game model and the
        link follow the game."""
        if game is None:
            return
        self.game = game
        self.game_model.sim_controller.set_game(game)
        self.game_model.set(game)
        self.link.set_game(game)

    def close_panel(self) -> None:
        if self.panel is None:
            return
        signal = GameUpdateSignal.get_instance()
        signal.gameupdated.disconnect(self.panel.setGame)
        signal.budgetupdated.disconnect(self.panel.budget_update)
        signal.gameupdated.disconnect(self.set_game)
        self.panel = None

    def pump(self) -> None:
        """Turns the Qt event loop."""
        self.app.processEvents()

    def wait_for(self, cond: Callable[[], bool]) -> None:
        """Turns the Qt event loop until `cond` holds."""

        def pumped() -> bool:
            self.pump()
            return cond()

        wait_until(pumped, timeout=DEADLINE)

    def live_maps(self) -> list[Any]:
        return [e.live_units for e in self.map_events if e.live_units is not None]


@pytest.fixture
def window(
    app: QApplication, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Window]]:
    """Opens the window on a fake companion, for `of` or the `game` fixture's
    game."""
    made: list[Window] = []

    def open_window(fake: FakeCompanion, of: Optional[Game] = None) -> Window:
        monkeypatch.setattr(
            liberation_install, "companion_address", lambda: fake.address
        )
        monkeypatch.setattr(liberation_install, "companion_token", lambda: TOKEN)
        w = Window(app, of if of is not None else request.getfixturevalue("game"))
        monkeypatch.setattr(EventStream, "put_nowait", w.map_events.append)
        made.append(w)
        w.start()
        return w

    yield open_window
    for w in made:
        w.close_panel()
        w.link.stop()
        w.pump()
    LiveStateContext.set(None)


def state(
    game: Game,
    minute: int,
    boot: str = "b1",
    own: bool = True,
    start: Optional[datetime] = None,
    model_time: Optional[float] = None,
) -> str:
    """A snapshot of the mission `boot` at its `minute`, the mission started at
    `start` (default 1 June 2015, 08:00), its model time `model_time` (default
    the minute's); the campaign's own carries its marker."""
    statics = [{"name": MARKER_PREFIX + str(game.live.marker), "id": 9000}]
    at = (start or datetime(2015, 6, 1, 8, 0)) + timedelta(minutes=minute)
    midnight = datetime(at.year, at.month, at.day)
    return json.dumps(
        {
            "minute": minute,
            "boot": boot,
            "session": 1,
            "modelTime": 60.0 * minute if model_time is None else model_time,
            "state": {
                "meta": {
                    "date": {"Year": at.year, "Month": at.month, "Day": at.day},
                    "absTime": (at - midnight).total_seconds(),
                },
                "groups": [],
                "statics": statics if own else [],
                "airbases": [],
            },
        }
    )


def plane(seq: int, kind: str, name: str) -> dict[str, Any]:
    return entry(seq, kind, unit={"name": name, "type": "F-16C_50", "side": 2})


def filler(seq: int) -> dict[str, Any]:
    """An entry that changes nothing: a takeoff of a name nobody knows."""
    return entry(seq, "takeoff", unit={"name": f"Other-{seq}", "type": "Su-25"})


def launched_flight(game: Game) -> Flight:
    """A flight launched into the accepted mission as `LIVE 1-1`, `LIVE 1-2`."""
    flight = plan_by_hand(game, 2)
    flight.launched = True
    flight.live_group_name = "LIVE 1"
    game.live.name_map.add_launched(flight, ["LIVE 1-1", "LIVE 1-2"])
    return flight


def following(game: Game) -> bool:
    """Whether the game follows the running mission (read afresh each time)."""
    return game.live.following_own_mission


def ato_flights(game: Game) -> list[Flight]:
    return [f for p in game.coalition_for(Player.BLUE).ato.packages for f in p.flights]


def loss_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The batch lines: `Live losses: <n> entries, ...`."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO and BATCH_LINE.match(r.getMessage())
    ]


def test_only_own_mission_followed(
    serve: Callable[..., FakeCompanion],
    window: Callable[[FakeCompanion], Window],
    game: Game,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(companionlinkcontroller, "STALE_S", 1.0)
    monkeypatch.setattr(companionlinkcontroller, "STALE_CHECK_MS", 50)

    foreign_gate, own_gate = threading.Event(), threading.Event()
    fake = serve(
        [
            event("state", state(game, 3)),
            foreign_gate,
            event("state", state(game, 4, boot="b2", own=False)),
            own_gate,
            event("state", state(game, 5)),
        ]
    )
    w = window(fake)

    w.wait_for(lambda: w.link.status_text() == "DCS Companion time: 3 minutes")
    own = LiveStateContext.get()
    assert own is not None and own.minute == 3
    assert following(game)
    tick = game.live.tick
    assert tick is not None and tick.minute == 3

    foreign_gate.set()
    w.wait_for(lambda: "another mission" in w.link.status_text())
    assert w.link.status_text() == FOLLOWED_NOT
    assert not following(game)
    assert game.live.tick is tick, "another mission's snapshot is not kept"
    held = LiveStateContext.get()
    assert held is not None and held.units == () and held.boot != "b2"
    [drawn, cleared] = w.live_maps()
    assert drawn is own and cleared.units == () and cleared.boot == ""
    clock = LiveStateContext.clock()
    held_at = clock.now()
    assert held_at is not None
    assert clock.now(time.monotonic() + 60.0) == held_at  # a minute on, no later

    own_gate.set()
    w.wait_for(lambda: w.link.status_text() == "DCS Companion time: 5 minutes")
    assert following(game)
    assert game.live.tick is not None and game.live.tick.minute == 5
    wall = time.monotonic()
    running, later = clock.now(wall), clock.now(wall + 60.0)
    assert running is not None and later is not None
    assert (later - running).total_seconds() == pytest.approx(60.0)

    # No snapshot for STALE_S: stale, not followed, the clock held.
    w.wait_for(
        lambda: w.link.status_text() == "Companion: waiting for a mission on the server"
    )
    assert not following(game)
    stale = clock.now()
    assert stale is not None
    assert clock.now(time.monotonic() + 60.0) == stale
    assert len(w.live_maps()) == 4  # own, cleared, own, cleared


def test_losses_applied_in_chunks(
    serve: Callable[..., FakeCompanion],
    window: Callable[[FakeCompanion], Window],
    game: Game,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    gate = threading.Event()
    fake = serve([event("state", state(game, 1)), gate, event("events", "{}")])
    w = window(fake)
    w.wait_for(lambda: following(game) and game.live.cursor == 0)
    flight = launched_flight(game)
    package = flight.package

    with fake.lock:
        fake.entries.extend(filler(seq) for seq in range(1, 600))
        fake.entries.append(plane(600, "dead", "LIVE 1-1"))
        fake.entries.append(plane(601, "land", "LIVE 1-2"))
    gate.set()
    w.wait_for(lambda: game.live.cursor == 601 and len(loss_lines(caplog)) == 2)

    first, second = loss_lines(caplog)
    assert first.startswith("Live losses: 500 entries, 0 dead, 0 captured, 0 flights")
    assert second.startswith(
        "Live losses: 101 entries, 1 dead, 0 captured, 1 flights finished, "
    )
    assert flight not in ato_flights(game)
    assert package not in game.blue.ato.packages
    assert any(flight.id in e.deleted_flights for e in w.map_events)
    squadron = flight.squadron
    held = sum(f.count for f in ato_flights(game) if f.squadron is squadron)
    assert squadron.untasked_aircraft == squadron.owned_aircraft - held


def test_failed_chunk_skipped_and_logged(
    serve: Callable[..., FakeCompanion],
    window: Callable[[FakeCompanion], Window],
    game: Game,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.INFO)
    apply = livelosses.apply_entries

    def failing_first_chunk(g: Game, entries: Any) -> Any:
        if entries[0].seq == 1:
            raise RuntimeError("a bad entry")
        return apply(g, entries)

    monkeypatch.setattr(livelosses, "apply_entries", failing_first_chunk)

    gate = threading.Event()
    fake = serve([event("state", state(game, 1)), gate, event("events", "{}")])
    w = window(fake)
    w.wait_for(lambda: following(game) and game.live.cursor == 0)
    launched_flight(game)

    with fake.lock:
        fake.entries.append(plane(1, "dead", "LIVE 1-1"))
        fake.entries.extend(filler(seq) for seq in range(2, 600))
        fake.entries.append(plane(600, "dead", "LIVE 1-2"))
    gate.set()
    w.wait_for(lambda: game.live.cursor == 600 and len(loss_lines(caplog)) == 1)

    [failure] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert failure.getMessage() == (
        "Live losses: applying entries 1..500 failed; skipped"
    )
    assert failure.exc_info is not None
    [line] = loss_lines(caplog)
    assert line.startswith("Live losses: 100 entries, 1 dead, ")
    assert "LIVE 1-2" in game.live.dead
    assert "LIVE 1-1" not in game.live.dead


def test_first_cursor_saved(
    serve: Callable[..., FakeCompanion],
    window: Callable[[FakeCompanion], Window],
    game: Game,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saves: list[Game] = []

    def autosave(g: Game) -> bool:
        saves.append(g)
        return True

    monkeypatch.setattr(persistency, "autosave", autosave)

    gate = threading.Event()
    fake = serve([event("state", state(game, 1)), gate, event("events", "{}")])
    w = window(fake)
    w.wait_for(lambda: following(game) and game.live.cursor == 0)
    assert saves == [game]

    with fake.lock:
        fake.entries.extend(filler(seq) for seq in range(1, 3))
    gate.set()
    w.wait_for(lambda: game.live.cursor == 2)
    assert saves == [game]


@pytest.fixture
def commands() -> Iterator[FakeCommands]:
    f = FakeCommands()
    f.start()
    yield f
    f.close()


def test_sent_flight_losses_applied_before_confirmed(
    serve: Callable[..., FakeCompanion],
    window: Callable[[FakeCompanion], Window],
    game: Game,
    commands: FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        liberation_install, "companion_command_address", lambda: commands.address
    )
    monkeypatch.setattr(
        liberation_install, "companion_command_token", lambda: COMMAND_TOKEN
    )
    commands.statuses = ["waiting"]  # accepted, never confirmed

    gate = threading.Event()
    fake = serve([event("state", state(game, 1)), gate, event("events", "{}")])
    w = window(fake)
    launcher = FlightLauncher(w.link)
    launcher.confirm_warn_s = launcher.confirm_cap_s = 1.0
    launcher.set_game(game)
    try:
        w.wait_for(lambda: following(game) and launcher.receiving)
        flight = plan_by_hand(game, 2)
        squadron = flight.squadron
        owned = squadron.owned_aircraft

        launcher.launch_now(flight)
        w.wait_for(lambda: len(commands.requests) == 1)
        assert launcher.is_busy(flight) and not flight.launched
        assert launcher.launching
        sent = commands.requests[0][0]["group"]["units"]
        names = [u["name"] for u in sent.values()]
        assert game.live.name_map.flight_units(flight) == names

        with fake.lock:
            fake.entries.append(plane(1, "dead", names[0]))
        gate.set()
        w.wait_for(lambda: game.live.cursor == 1)

        assert names[0] in game.live.dead
        assert squadron.owned_aircraft == owned - 1
    finally:
        launcher.stop()


def test_spawn_confirmed_after_mission_turned_foreign(
    serve: Callable[..., FakeCompanion],
    window: Callable[[FakeCompanion], Window],
    game: Game,
    commands: FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        liberation_install, "companion_command_address", lambda: commands.address
    )
    monkeypatch.setattr(
        liberation_install, "companion_command_token", lambda: COMMAND_TOKEN
    )
    commands.statuses = ["waiting"]  # accepted, not confirmed yet
    flight = plan_by_hand(game, 2)
    start = flight.flight_plan.startup_time()  # due from the first snapshot

    foreign_gate, own_gate = threading.Event(), threading.Event()
    fake = serve(
        [
            event("state", state(game, 1, start=start)),
            foreign_gate,
            event("state", state(game, 1, boot="b2", own=False, start=start)),
            own_gate,
            event("state", state(game, 2, start=start)),
        ]
    )
    w = window(fake)
    launcher = FlightLauncher(w.link)
    launcher.confirm_warn_s = launcher.confirm_cap_s = 30.0
    launcher.confirm_poll_s = 0.05
    ended: list[tuple[Flight, str]] = []
    launcher.launch_failed.connect(lambda f, message: ended.append((f, message)))
    launcher.set_game(game)
    try:
        w.wait_for(lambda: following(game) and launcher.receiving)
        launcher.launch_now(flight)
        w.wait_for(lambda: len(commands.requests) == 1)

        foreign_gate.set()
        w.wait_for(lambda: "another mission" in w.link.status_text())
        with commands.lock:
            commands.statuses = ["spawned"]
        w.wait_for(lambda: not launcher.is_busy(flight))

        assert flight.launched is False
        assert flight.live_group_name is None
        said = (
            f"{flight} spawned after the campaign's mission stopped being "
            "followed; it launches again"
        )
        assert f"Launch: {said}" in [r.getMessage() for r in caplog.records]
        assert ended == [(flight, said)]  # the launch ends on screen

        with commands.lock:
            commands.statuses = ["waiting"]
        own_gate.set()
        w.wait_for(lambda: len(commands.requests) == 2)
        names = [u["name"] for u in commands.requests[1][0]["group"]["units"].values()]
        assert game.live.name_map.flight_units(flight) == names
        with commands.lock:
            commands.statuses = ["spawned"]
        w.wait_for(lambda: flight.launched)
    finally:
        launcher.stop()


def test_edit_flight_dialog_defers_removal(
    serve: Callable[..., FakeCompanion],
    window: Callable[[FakeCompanion], Window],
    game: Game,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = threading.Event()
    fake = serve([event("state", state(game, 1)), gate, event("events", "{}")])
    w = window(fake)
    w.wait_for(lambda: following(game) and game.live.cursor == 0)
    monkeypatch.setattr(Dialog, "game_model", w.game_model)
    monkeypatch.setattr(Dialog, "edit_flight_dialog", None)

    uiconstants.load_event_icons()  # the dialog's window icon
    # Opened before the launch: a launched flight's dialog does not open.
    flight = plan_by_hand(game, 2)
    package_model: PackageModel = w.game_model.ato_model.package_models.acquire(
        flight.package
    )
    Dialog.open_edit_flight_dialog(package_model, flight)
    dialog = Dialog.edit_flight_dialog
    assert dialog is not None and dialog.isVisible()
    flight.launched = True
    flight.live_group_name = "LIVE 1"
    game.live.name_map.add_launched(flight, ["LIVE 1-1", "LIVE 1-2"])

    with fake.lock:
        fake.entries.append(plane(1, "dead", "LIVE 1-1"))
        fake.entries.append(plane(2, "dead", "LIVE 1-2"))
    gate.set()
    w.wait_for(lambda: game.live.cursor == 2)
    assert flight in ato_flights(game)

    dialog.reject()
    w.pump()
    assert flight not in ato_flights(game)
    assert any(flight.id in e.deleted_flights for e in w.map_events)


def test_package_dialog_defers_finished_flight(
    serve: Callable[..., FakeCompanion],
    window: Callable[[FakeCompanion], Window],
    game: Game,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = threading.Event()
    fake = serve([event("state", state(game, 1)), gate, event("events", "{}")])
    w = window(fake)
    w.wait_for(lambda: following(game) and game.live.cursor == 0)
    monkeypatch.setattr(Dialog, "game_model", w.game_model)
    monkeypatch.setattr(Dialog, "edit_package_dialog", None)

    uiconstants.load_event_icons()  # the dialog's window icon
    flight = launched_flight(game)
    squadron = flight.squadron
    package_model: PackageModel = w.game_model.ato_model.package_models.acquire(
        flight.package
    )
    Dialog.open_edit_package_dialog(package_model)
    dialog = Dialog.edit_package_dialog
    assert dialog is not None and dialog.isVisible()

    with fake.lock:
        fake.entries.append(plane(1, "dead", "LIVE 1-1"))
        fake.entries.append(plane(2, "land", "LIVE 1-2"))
    gate.set()
    w.wait_for(lambda: game.live.cursor == 2)
    assert flight in ato_flights(game)
    assert not any(flight.id in e.deleted_flights for e in w.map_events)

    dialog.reject()
    w.pump()
    assert flight not in ato_flights(game)
    assert any(flight.id in e.deleted_flights for e in w.map_events)
    held = sum(f.count for f in ato_flights(game) if f.squadron is squadron)
    assert squadron.untasked_aircraft == squadron.owned_aircraft - held


def test_drain_stops_when_nothing_held(
    serve: Callable[..., FakeCompanion],
    window: Callable[[FakeCompanion], Window],
    game: Game,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns: list[Callable[[], None]] = []

    def single_shot(_ms: int, slot: Callable[[], None]) -> None:
        turns.append(slot)

    monkeypatch.setattr(livelosses.QTimer, "singleShot", single_shot)

    gate = threading.Event()
    fake = serve([event("state", state(game, 1)), gate, event("events", "{}")])
    w = window(fake)
    w.wait_for(lambda: following(game) and game.live.cursor == 0)

    with fake.lock:
        fake.entries.extend(filler(seq) for seq in range(1, 1201))
    gate.set()
    w.wait_for(lambda: game.live.cursor == 500)
    assert len(turns) == 1
    turns[0]()
    assert game.live.cursor == 1000 and len(turns) == 2
    turns[1]()
    assert game.live.cursor == 1200 and len(turns) == 2


def test_spawn_confirmed_while_link_stale_launched(
    serve: Callable[..., FakeCompanion],
    window: Callable[[FakeCompanion], Window],
    game: Game,
    commands: FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stale link may still carry the campaign's mission: a spawn confirmed
    then is launched, never sent a second time."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(companionlinkcontroller, "STALE_S", 1.0)
    monkeypatch.setattr(companionlinkcontroller, "STALE_CHECK_MS", 50)
    monkeypatch.setattr(
        liberation_install, "companion_command_address", lambda: commands.address
    )
    monkeypatch.setattr(
        liberation_install, "companion_command_token", lambda: COMMAND_TOKEN
    )
    commands.statuses = ["waiting"]
    fake = serve([event("state", state(game, 1))])
    w = window(fake)
    launcher = FlightLauncher(w.link)
    launcher.confirm_warn_s = launcher.confirm_cap_s = 30.0
    launcher.confirm_poll_s = 0.05
    launcher.set_game(game)
    try:
        w.wait_for(lambda: following(game) and launcher.receiving)
        flight = plan_by_hand(game, 2)
        launcher.launch_now(flight)
        w.wait_for(lambda: len(commands.requests) == 1)

        w.wait_for(
            lambda: w.link.status_text()
            == "Companion: waiting for a mission on the server"
        )
        assert not following(game)
        with commands.lock:
            commands.statuses = ["spawned"]
        w.wait_for(lambda: not launcher.is_busy(flight))

        assert flight.launched is True
        assert not [r for r in caplog.records if "it launches again" in r.getMessage()]
        assert len(commands.requests) == 1
    finally:
        launcher.stop()


def test_launcher_refuses_foreign_mission(
    serve: Callable[..., FakeCompanion],
    window: Callable[[FakeCompanion], Window],
    game: Game,
    commands: FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        liberation_install, "companion_command_address", lambda: commands.address
    )
    monkeypatch.setattr(
        liberation_install, "companion_command_token", lambda: COMMAND_TOKEN
    )
    commands.statuses = ["spawned"]
    foreign_gate = threading.Event()
    fake = serve(
        [
            event("state", state(game, 1)),
            foreign_gate,
            event("state", state(game, 1, boot="b2", own=False)),
        ]
    )
    w = window(fake)
    launcher = FlightLauncher(w.link)
    launcher.set_game(game)
    try:
        w.wait_for(lambda: following(game) and launcher.receiving)
        foreign_gate.set()
        w.wait_for(lambda: "another mission" in w.link.status_text())
        assert launcher.receiving
        flight = plan_by_hand(game, 2)

        launcher.launch_now(flight)
        w.pump()

        said = (
            f"Launch: the running mission is not this campaign's; {flight} "
            "not launched"
        )
        assert said in [r.getMessage() for r in caplog.records]
        assert not launcher.is_busy(flight) and not flight.launched
        assert not launcher.launching
        assert game.live.name_map.flight_units(flight) == []
        assert commands.requests == []
    finally:
        launcher.stop()


def test_autosave_every_ten_minutes(
    serve: Callable[..., FakeCompanion],
    window: Callable[[FakeCompanion], Window],
    game: Game,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    gate = threading.Event()
    fake = serve(
        [
            event("state", state(game, 3)),
            gate,
            event("state", state(game, 4, boot="b2", own=False)),
        ]
    )
    w = window(fake)
    now = [1000.0]
    busy = [False]
    autosave = DynamicAutosave(w.link, lambda: busy[0], clock=lambda: now[0])
    autosave.set_game(game)
    path = persistency.dynamic_autosave_path()

    def at(seconds: float) -> bool:
        """The timer's check `seconds` after the game was set; whether the
        autosave is there."""
        now[0] = 1000.0 + seconds
        autosave.check()
        return path.exists()

    def autosave_lines() -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if AUTOSAVE_LINE.fullmatch(r.getMessage())
        ]

    try:
        w.wait_for(lambda: following(game) and game.live.cursor == 0)
        # Live losses autosaved the first cursor; the timer's are checked here.
        path.unlink(missing_ok=True)

        assert not at(599)
        busy[0] = True
        assert not at(600), "a flight is launching"
        busy[0] = False
        assert not at(659)
        assert at(660)

        saved = persistency.load_game(str(path))
        assert saved is not None and saved.dynamic_campaign
        assert saved.live.marker == game.live.marker
        assert saved.live.tick is not None and saved.live.tick.minute == 3
        [line] = autosave_lines()
        match = AUTOSAVE_LINE.fullmatch(line)
        assert match is not None and match.group(1) == str(path)

        gate.set()
        w.wait_for(lambda: not following(game))
        path.unlink()
        assert not at(1300), "another mission is running"
        assert len(autosave_lines()) == 1
    finally:
        autosave.set_game(None)


class Boxes:
    """`QMessageBox`'s static methods, recorded as (method, title, text); a
    question is answered `answer`."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.shown: list[tuple[str, str, str]] = []
        self.answer = QMessageBox.StandardButton.No
        for method in ("question", "information", "warning", "critical"):
            monkeypatch.setattr(QMessageBox, method, self._recorder(method))

    def _recorder(self, method: str) -> Any:
        def show(_parent: Any, title: str, text: str, *_args: Any) -> Any:
            self.shown.append((method, title, text))
            if method == "question":
                return self.answer
            return QMessageBox.StandardButton.Ok

        return staticmethod(show)


@pytest.fixture
def boxes(monkeypatch: pytest.MonkeyPatch) -> Boxes:
    return Boxes(monkeypatch)


def use_commands(monkeypatch: pytest.MonkeyPatch, commands: FakeCommands) -> None:
    """Launches go to the fake command port."""
    monkeypatch.setattr(
        liberation_install, "companion_command_address", lambda: commands.address
    )
    monkeypatch.setattr(
        liberation_install, "companion_command_token", lambda: COMMAND_TOKEN
    )


def test_spawn_id_recorded_when_asked(
    serve: Callable[..., FakeCompanion],
    window: Callable[[FakeCompanion], Window],
    game: Game,
    commands: FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_commands(monkeypatch, commands)
    commands.spawn_id = "s-7"
    commands.statuses = ["waiting"]  # accepted, never confirmed
    w = window(serve([event("state", state(game, 1))]))
    launcher = FlightLauncher(w.link)
    launcher.confirm_warn_s = launcher.confirm_cap_s = 30.0
    launcher.confirm_poll_s = 0.05
    launcher.set_game(game)
    try:
        w.wait_for(lambda: following(game) and launcher.receiving)
        flight = plan_by_hand(game, 2)
        launcher.launch_now(flight)

        def saved_spawn() -> Optional[int]:
            game.live.mark_saved(LiveStateContext.clock().model_time())
            assert game.live.saved_at is not None
            return game.live.saved_at.spawn

        w.wait_for(lambda: saved_spawn() == 7)
        assert launcher.is_busy(flight) and not flight.launched
    finally:
        with commands.lock:
            commands.statuses = ["failed"]  # the worker ends
        launcher.stop()


class NoMap(QWidget):
    """The main window's map without its web view (Qt WebEngine does not run
    on the offscreen platform here)."""

    def __init__(self, *_args: Any) -> None:
        super().__init__()


@pytest.fixture
def main_window(
    app: QApplication,
    boxes: Boxes,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[Callable[..., QLiberationWindow]]:
    """Opens Retribution's main window on `game`, its link on `fake` (none
    for a game without a link); its geometry kept in `tmp_path`."""
    made: list[QLiberationWindow] = []
    monkeypatch.setattr(liberation_window, "QLiberationMap", NoMap)
    settings = str(tmp_path / "qt-ui.ini")
    monkeypatch.setattr(
        QLiberationWindow,
        "_qsettings",
        lambda _self: QSettings(settings, QSettings.Format.IniFormat),
    )
    monkeypatch.setattr(EventStream, "put_nowait", lambda _events: None)
    liberation_theme.set_theme_index(liberation_theme.DEFAULT_THEME_INDEX)
    uiconstants.load_icons()
    if GameUpdateSignal.get_instance() is None:
        GameUpdateSignal()

    def open_window(fake: Optional[FakeCompanion], game: Game) -> QLiberationWindow:
        if fake is not None:
            monkeypatch.setattr(
                liberation_install, "companion_address", lambda: fake.address
            )
            monkeypatch.setattr(liberation_install, "companion_token", lambda: TOKEN)
        win = QLiberationWindow(
            game, UiFlags(dev_ui_webserver=False, show_sim_speed_controls=False)
        )
        made.append(win)
        return win

    yield open_window
    signal = GameUpdateSignal.get_instance()
    for win in made:
        signal.gameupdated.disconnect(win.setGame)
        signal.debriefingReceived.disconnect(win.onDebriefing)
        signal.game_state_changed.disconnect(win.onEndGame)
        signal.gameupdated.disconnect(win.top_panel.setGame)
        signal.budgetupdated.disconnect(win.top_panel.budget_update)
        win.dynamic_autosave.set_game(None)
        win.flight_launcher.stop()
        win.companion_link.stop()
        # Gone, so a later window's close does not close it again.
        win.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    LiveStateContext.set(None)


def pumped_until(app: QApplication, cond: Callable[[], bool]) -> None:
    """Turns the Qt event loop until `cond` holds."""

    def pumped() -> bool:
        app.processEvents()
        return cond()

    wait_until(pumped, timeout=DEADLINE)


def autosave_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if AUTOSAVE_LINE.fullmatch(r.getMessage())
    ]


def mtime(path: Path) -> Optional[int]:
    return path.stat().st_mtime_ns if path.exists() else None


def test_autosave_after_launch(
    app: QApplication,
    serve: Callable[..., FakeCompanion],
    main_window: Callable[..., QLiberationWindow],
    game: Game,
    commands: FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    use_commands(monkeypatch, commands)
    commands.statuses = ["waiting"]
    win = main_window(serve([event("state", state(game, 1))]), game)
    launcher = win.flight_launcher
    launcher.confirm_poll_s = 0.05
    path = persistency.dynamic_autosave_path()
    pumped_until(app, lambda: following(game) and game.live.cursor == 0)
    pumped_until(app, lambda: launcher.receiving)
    before = mtime(path)  # live losses saved the first cursor
    flight = plan_by_hand(game, 2)

    launcher.launch_now(flight)
    pumped_until(app, lambda: len(commands.requests) == 1)
    app.processEvents()
    assert mtime(path) == before, "not while the flight is launching"
    with commands.lock:
        commands.statuses = ["spawned"]
    pumped_until(app, lambda: flight.launched)

    assert mtime(path) != before
    saved = persistency.load_game(str(path))
    assert saved is not None
    [in_flight] = [f for f in ato_flights(saved) if f.launched]
    assert in_flight.live_group_name == flight.live_group_name is not None
    assert len(autosave_lines(caplog)) == 1


def test_autosave_on_quit(
    app: QApplication,
    serve: Callable[..., FakeCompanion],
    main_window: Callable[..., QLiberationWindow],
    game: Game,
    boxes: Boxes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    win = main_window(serve([event("state", state(game, 1))]), game)
    path = persistency.dynamic_autosave_path()
    pumped_until(app, lambda: following(game) and game.live.cursor == 0)
    path.unlink()  # live losses saved the first cursor

    boxes.answer = QMessageBox.StandardButton.No
    win.close()

    assert [b[1] for b in boxes.shown] == ["Quit Retribution?"]
    saved = persistency.load_game(str(path))
    assert saved is not None and saved.live.marker == game.live.marker
    assert len(autosave_lines(caplog)) == 1


def test_autosave_on_quit_waits_for_launch(
    app: QApplication,
    serve: Callable[..., FakeCompanion],
    main_window: Callable[..., QLiberationWindow],
    game: Game,
    commands: FakeCommands,
    boxes: Boxes,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A launch in progress for longer than `QUIT_WAIT_S` is waited for that
    long, then the autosave is written anyway."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(dynamicautosave, "QUIT_WAIT_S", 0.2)
    use_commands(monkeypatch, commands)
    commands.statuses = ["waiting"]  # accepted, not confirmed
    win = main_window(serve([event("state", state(game, 1))]), game)
    launcher = win.flight_launcher
    launcher.confirm_warn_s = launcher.confirm_cap_s = 30.0
    launcher.confirm_poll_s = 0.05
    path = persistency.dynamic_autosave_path()
    pumped_until(app, lambda: following(game) and game.live.cursor == 0)
    pumped_until(app, lambda: launcher.receiving)
    flight = plan_by_hand(game, 2)
    launcher.launch_now(flight)
    pumped_until(app, lambda: len(commands.requests) == 1)
    path.unlink()  # live losses saved the first cursor

    boxes.answer = QMessageBox.StandardButton.No
    started = time.monotonic()
    try:
        win.close()
        waited = time.monotonic() - started
    finally:
        with commands.lock:
            commands.statuses = ["failed"]  # the worker ends

    assert waited >= 0.2
    assert path.exists()
    assert len(autosave_lines(caplog)) == 1
    messages = [r.getMessage() for r in caplog.records]
    assert "Autosave: a flight is still launching after 0.2 s; saving anyway" in (
        messages
    )


def test_quit_normal_campaign_no_dynamic_autosave(
    app: QApplication,
    saved_games: Path,
    main_window: Callable[..., QLiberationWindow],
    boxes: Boxes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    game = new_game(dynamic=False, squadrons_start_full=True, campaign_file=CAMPAIGN)
    game.pass_turn(no_action=True)
    persistency.dynamic_autosave_path().unlink(missing_ok=True)
    win = main_window(None, game)

    boxes.answer = QMessageBox.StandardButton.No
    win.close()

    assert [b[1] for b in boxes.shown] == ["Quit Retribution?"]
    assert not persistency.dynamic_autosave_path().exists()
    assert autosave_lines(caplog) == []


# The pick-up: a loaded save follows the running mission only when it is the
# save's own, has not run on more than five minutes and holds no spawn the
# save does not know.

#: The status while the pick-up waits for the command port.
AWAITING = "Companion: waiting for the command port to check the running mission"


class FollowedSave:
    """A save of the `game` fixture's game, made while it followed its mission
    `b1`/1 at model time 1000: `LIVE 1` launched into it as spawn `s-4`,
    another flight planned and due, the event log's cursor at 0."""

    def __init__(self, game: Game, path: Path) -> None:
        live = game.live
        planned = plan_by_hand(game, 2)
        #: The mission's start: both flights are due from it.
        self.start = planned.flight_plan.startup_time()
        running = parse_snapshot(state(game, 16, start=self.start, model_time=1000.0))
        live.receive(EventBatch((), through=0))
        assert live.decide(running) is MissionVerdict.OWN
        flight = launched_flight(game)
        assert flight.flight_plan.startup_time() <= self.start
        live.spawn_asked(4)
        clock = LiveStateContext.clock()
        clock.update(running, 0.0)
        clock.hold(0.0)
        try:
            self.game = save_and_load(game, path)
        finally:
            LiveStateContext.set(None)
        assert self.game.live.awaiting_pickup
        assert self.game.live.saved_at == SavedAt(("b1", 1), 1000.0, 4)
        [self.launched] = [f for f in ato_flights(self.game) if f.launched]
        [self.planned] = [f for f in ato_flights(self.game) if not f.launched]

    def running(self, minute: int, model_time: float) -> str:
        """The snapshot of the saved mission at `minute`, its model time
        `model_time`."""
        return event(
            "state",
            state(self.game, minute, start=self.start, model_time=model_time),
        )


@pytest.fixture
def followed_save(game: Game, tmp_path: Path) -> FollowedSave:
    return FollowedSave(game, tmp_path / "followed.retribution")


def launcher_on(w: Window, game: Game) -> FlightLauncher:
    """A launcher on the window's link for `game`, following each spawn up to
    30 s."""
    launcher = FlightLauncher(w.link)
    launcher.confirm_warn_s = launcher.confirm_cap_s = 30.0
    launcher.confirm_poll_s = 0.05
    launcher.set_game(game)
    return launcher


def test_pickup_within_five_minutes(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    followed_save: FollowedSave,
    commands: FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(flightlauncher, "CHECK_INTERVAL_MS", 3_600_000)
    use_commands(monkeypatch, commands)
    commands.spawns = [(4, "LIVE 1", "spawned"), (5, "Other 1", "failed")]
    commands.spawns_mission = ("b1", 1, 1180.0)
    commands.statuses = ["waiting"]
    loaded = followed_save.game
    fake = serve([followed_save.running(3, 1180.0)])
    with fake.lock:
        fake.entries.append(plane(1, "dead", "LIVE 1-1"))

    w = window(fake, loaded)
    launcher = launcher_on(w, loaded)
    try:
        w.wait_for(lambda: following(loaded) and "LIVE 1-1" in loaded.live.dead)
        assert w.link.status_text() == (
            "DCS Companion time: 3 minutes\n"
            "Picked up the running mission, 3 minutes after the save"
        )
        assert w.link.pickup_note == (
            "Picked up the running mission, 3 minutes after the save"
        )
        assert loaded.live.cursor == 1
        assert "Pick-up: picked up boot b1 session 1, 180 s after the save" in (
            r.getMessage() for r in caplog.records
        )
        assert len(commands.lists) == 1, "the check asks once"

        w.wait_for(lambda: launcher.receiving)
        launcher.check()
        w.wait_for(lambda: len(commands.requests) == 1)
        launcher.check()
        w.pump()
        [(sent, _)] = commands.requests
        names = [u["name"] for u in sent["group"]["units"].values()]
        assert loaded.live.name_map.flight_units(followed_save.planned) == names
        launched = followed_save.launched
        assert launched.launched and launched.live_group_name == "LIVE 1"
        assert not launcher.is_busy(launched)
    finally:
        with commands.lock:
            commands.statuses = ["failed"]  # the worker ends
        launcher.stop()


def test_pickup_refused_too_late(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    followed_save: FollowedSave,
    commands: FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(flightlauncher, "CHECK_INTERVAL_MS", 3_600_000)
    use_commands(monkeypatch, commands)
    commands.spawns = [(4, "LIVE 1", "spawned")]
    commands.spawns_mission = ("b1", 1, 1400.0)
    loaded = followed_save.game
    fake = serve([followed_save.running(3, 1400.0)])
    with fake.lock:
        fake.entries.append(plane(1, "dead", "LIVE 1-1"))

    w = window(fake, loaded)
    launcher = launcher_on(w, loaded)
    try:
        w.wait_for(lambda: loaded.live.cursor == 1 and launcher.receiving)
        assert w.link.status_text() == (
            "Companion: the server's mission is 6 minutes past this save; "
            "Continue to load the campaign's mission"
        )
        assert w.link.pickup_note is None
        assert not following(loaded) and not loaded.live.awaiting_pickup
        assert "LIVE 1-1" not in loaded.live.dead
        assert LiveStateContext.get() is None, "the mission is not drawn"
        assert w.live_maps() == []

        launcher.check()
        w.pump()
        assert commands.requests == []
        assert not followed_save.planned.launched
    finally:
        launcher.stop()


def test_pickup_waits_for_command_port(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    followed_save: FollowedSave,
    commands: FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_commands(monkeypatch, commands)
    commands.spawns = [(4, "LIVE 1", "spawned")]
    commands.spawns_mission = ("b1", 1, 1180.0)
    assert commands.server is not None
    port = commands.server.server_address[1]
    commands.close()  # the companion restarting
    loaded = followed_save.game
    gate = threading.Event()
    fake = serve(
        [
            followed_save.running(3, 1180.0),
            gate,
            followed_save.running(4, 1240.0),
        ]
    )
    with fake.lock:
        fake.entries.append(plane(1, "dead", "LIVE 1-1"))

    w = window(fake, loaded)
    w.wait_for(lambda: w.link.status_text() == AWAITING)
    assert not following(loaded) and loaded.live.awaiting_pickup
    assert LiveStateContext.get() is None
    w.wait_for(lambda: loaded.live.has_held)
    w.pump()
    assert loaded.live.cursor == 0 and "LIVE 1-1" not in loaded.live.dead

    commands.start(port)
    gate.set()
    w.wait_for(lambda: following(loaded) and "LIVE 1-1" in loaded.live.dead)
    assert w.link.status_text() == (
        "DCS Companion time: 4 minutes\n"
        "Picked up the running mission, 3 minutes after the save"
    )
    assert len(commands.lists) == 1


def test_spawn_confirmed_into_moved_on_mission(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    game: Game,
    commands: FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A spawn sent into the campaign's mission, confirmed after a pick-up
    check refused that mission, leaves its flight not launched: it launches
    again into the campaign's next mission."""
    caplog.set_level(logging.INFO)
    use_commands(monkeypatch, commands)
    commands.statuses = ["waiting"]  # accepted, not confirmed yet
    flight = plan_by_hand(game, 2)
    start = flight.flight_plan.startup_time()  # due from the first snapshot

    moved_gate, next_gate = threading.Event(), threading.Event()
    script: list[Item] = [
        event("state", state(game, 1, start=start)),
        moved_gate,
        event("state", state(game, 2, start=start)),
        next_gate,
        "",  # the campaign's next mission, under its new marker
    ]
    w = window(serve(script))
    launcher = launcher_on(w, game)
    ended: list[tuple[Flight, str]] = []
    launcher.launch_failed.connect(lambda f, message: ended.append((f, message)))
    try:
        w.wait_for(lambda: following(game) and launcher.receiving)
        launcher.launch_now(flight)
        w.wait_for(lambda: len(commands.requests) == 1)

        # The check refuses the running mission: it has run on 400 s.
        running = LiveStateContext.get()
        assert running is not None
        game.live.mark_saved(60.0)
        refused = game.live.pick_up(
            running, SpawnsView(running.boot, running.session, 460.0, ())
        )
        assert refused.decided and not refused.picked
        moved_gate.set()
        w.wait_for(lambda: "past this save" in w.link.status_text())
        assert game.live.current_verdict is MissionVerdict.MOVED_ON
        with commands.lock:
            commands.statuses = ["spawned"]
        w.wait_for(lambda: not launcher.is_busy(flight))

        assert flight.launched is False
        assert flight.live_group_name is None
        said = (
            f"{flight} spawned after the campaign's mission stopped being "
            "followed; it launches again"
        )
        assert f"Launch: {said}" in [r.getMessage() for r in caplog.records]
        assert ended == [(flight, said)]

        with commands.lock:
            commands.statuses = ["waiting"]
        game.live.new_marker()  # as Continue writes the campaign's mission
        script[4] = event("state", state(game, 1, boot="b2", start=start))
        next_gate.set()
        w.wait_for(lambda: len(commands.requests) == 2)
        names = [u["name"] for u in commands.requests[1][0]["group"]["units"].values()]
        assert game.live.name_map.flight_units(flight) == names
        with commands.lock:
            commands.statuses = ["spawned"]
        w.wait_for(lambda: flight.launched)
    finally:
        launcher.stop()
