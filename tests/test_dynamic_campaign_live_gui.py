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
from pathlib import Path
from typing import Any, Callable

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from game import persistency  # noqa: E402
from game.ato.flight import Flight  # noqa: E402
from game.companionlink.livestate import LiveStateContext  # noqa: E402
from game.companionlink.liverecord import MARKER_PREFIX  # noqa: E402
from game.companionlink.liveunitmap import LiveUnitMap  # noqa: E402
from game.game import Game  # noqa: E402
from game.server import EventStream  # noqa: E402
from game.sim import GameUpdateEvents  # noqa: E402
from game.theater import Player  # noqa: E402
from qt_ui import companionlinkcontroller, liberation_install, livelosses  # noqa: E402
from qt_ui.companionlinkcontroller import CompanionLinkController  # noqa: E402
from qt_ui.flightlauncher import FlightLauncher  # noqa: E402
from qt_ui.livelosses import LiveLosses  # noqa: E402
from qt_ui.models import GameModel  # noqa: E402
from qt_ui.simcontroller import SimController  # noqa: E402
from tests.companionlink.test_commands import TOKEN as COMMAND_TOKEN  # noqa: E402
from tests.companionlink.test_commands import FakeCommands  # noqa: E402
from tests.companionlink.test_link import (  # noqa: E402
    TOKEN,
    FakeCompanion,
    entry,
    event,
    fakes,  # noqa: F401 (the fixture)
    serve,  # noqa: F401 (the fixture)
)
from tests.test_dynamic_campaign_air_wing import (  # noqa: E402
    generate_starting_mission,
    new_game,
    plan_by_hand,
    saved_games,  # noqa: F401 (the fixture)
)

CAMPAIGN = Path("resources/campaigns/golan_heights_lite.yaml")
DEADLINE = 5.0
BATCH_LINE = re.compile(r"Live losses: \d+ entries, ")


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
    """The main window's link controller and live losses for a game."""

    def __init__(self, app: QApplication, game: Game) -> None:
        self.app = app
        self.game = game
        self.game_model = GameModel(game, SimController(game))
        self.link = CompanionLinkController()
        self.losses = LiveLosses(self.link)
        self.losses.set_game(self.game_model)
        self.map_events: list[GameUpdateEvents] = []

    def start(self) -> None:
        EventStream.drain()
        self.link.set_game(self.game)

    def pump(self) -> None:
        """Turns the Qt event loop and collects the map's events."""
        self.app.processEvents()
        while not EventStream._queue.empty():
            self.map_events.append(EventStream._queue.get_nowait())
            EventStream._queue.task_done()

    def wait_for(self, cond: Callable[[], bool]) -> None:
        end = time.monotonic() + DEADLINE
        while True:
            self.pump()
            if cond():
                return
            if time.monotonic() > end:
                raise AssertionError("timed out")
            time.sleep(0.01)

    def live_maps(self) -> list[Any]:
        return [e.live_units for e in self.map_events if e.live_units is not None]


@pytest.fixture
def window(
    app: QApplication, game: Game, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[FakeCompanion], Window]]:
    made: list[Window] = []

    def open_window(fake: FakeCompanion) -> Window:
        monkeypatch.setattr(
            liberation_install, "companion_address", lambda: fake.address
        )
        monkeypatch.setattr(liberation_install, "companion_token", lambda: TOKEN)
        w = Window(app, game)
        made.append(w)
        w.start()
        return w

    yield open_window
    for w in made:
        w.link.stop()
        w.pump()
    LiveStateContext.set(None)


def state(game: Game, minute: int, boot: str = "b1", own: bool = True) -> str:
    """A snapshot of the mission `boot`; the campaign's own carries its marker."""
    statics = [{"name": MARKER_PREFIX + str(game.live.marker), "id": 9000}]
    return json.dumps(
        {
            "minute": minute,
            "boot": boot,
            "session": 1,
            "modelTime": 60.0 * minute,
            "state": {
                "meta": {
                    "date": {"Year": 2015, "Month": 6, "Day": 1},
                    "absTime": 28800.0 + 60 * minute,
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

    w.wait_for(lambda: w.link.status_text() == "Companion: minute 3, 0 units")
    own = LiveStateContext.get()
    assert own is not None and own.minute == 3
    assert following(game)

    foreign_gate.set()
    w.wait_for(lambda: "another mission" in w.link.status_text())
    assert w.link.status_text() == (
        "Companion: another mission is running; waiting for this campaign's"
    )
    assert not following(game)
    held = LiveStateContext.get()
    assert held is not None and held.units == () and held.boot != "b2"
    [drawn, cleared] = w.live_maps()
    assert drawn is own and cleared.units == () and cleared.boot == ""
    clock = LiveStateContext.clock()
    now = clock.now()
    time.sleep(0.05)
    assert clock.now() == now

    own_gate.set()
    w.wait_for(lambda: w.link.status_text() == "Companion: minute 5, 0 units")
    assert following(game)
    running = clock.now()
    time.sleep(0.05)
    later = clock.now()
    assert running is not None and later is not None and later > running

    # No snapshot for STALE_S: stale, not followed, the clock held.
    w.wait_for(
        lambda: w.link.status_text() == "Companion: waiting for a mission on the server"
    )
    assert not following(game)
    stale = clock.now()
    time.sleep(0.05)
    assert clock.now() == stale
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
