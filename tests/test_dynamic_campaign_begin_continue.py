"""A dynamic campaign's Begin and Continue (`CampaignMission`), against the
fake companion's two ports.

Begin passes the turn and writes the campaign's starting mission; Continue
writes the campaign's mission again, from its tick (slice 2's restore) or, with
none, from the game as it stands. Either writes under a dated name in DCS's
`Missions/`, asks the companion to load it (`FakeCommands` records the loads)
and waits for the new mission. Dialogs are answered through `Boxes`; the
wait's clock is the test's. What is checked is what the user and the server
get: the dialogs, the file, the load asked, the status, the log and the game.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import zipfile
import zlib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Callable, Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from game import persistency  # noqa: E402
from game.companionlink.liverecord import MARKER_PREFIX, MissionVerdict  # noqa: E402
from game.companionlink.livestate import LiveStateContext  # noqa: E402
from game.companionlink.launching import due_flights  # noqa: E402
from game.companionlink.restore import restore_start  # noqa: E402
from game.companionlink.snapshot import parse_snapshot  # noqa: E402
from game.game import Game  # noqa: E402
from qt_ui import campaignmission, companionlinkcontroller, flightlauncher  # noqa: E402
from qt_ui.campaignmission import CampaignMission  # noqa: E402
from qt_ui.flightlauncher import FlightLauncher  # noqa: E402
from qt_ui.windows.GameUpdateSignal import GameUpdateSignal  # noqa: E402
from tests.conftest import (  # noqa: E402
    FakeCommands,
    FakeCompanion,
    Item,
    event,
    new_game,
    plan_by_hand,
    save_and_load,
)
from tests.test_dynamic_campaign_live_gui import (  # noqa: E402,F401
    CAMPAIGN,
    FOLLOWED_NOT,
    Boxes,
    Window,
    app,
    boxes,
    commands,
    following,
    game,
    state,
    use_commands,
    window,
)

OK = QMessageBox.StandardButton.Ok
CANCEL = QMessageBox.StandardButton.Cancel
#: A mission file's dated name.
DATED = re.compile(r"retribution_dynamic_\d{8}_\d{4}(_\d+)?\.miz")
#: The end of every dialog that loads a mission.
DROPPED = "The mission running on the server ends; players go back to slot selection."
BEGIN = (
    "question",
    "Begin the campaign?",
    "Retribution writes the campaign's starting mission and DCS Companion loads "
    f"it on the server. {DROPPED}",
)
#: Continue without a tick.
CONTINUE_NOW = (
    "question",
    "Continue the campaign?",
    "Retribution writes the campaign's mission as it stands now and DCS "
    f"Companion loads it on the server. {DROPPED}",
)


def continue_from(at: str) -> tuple[str, str, str]:
    """Continue's dialog with a tick seen at `at` (`YYYY-MM-DD HH:MM`)."""
    return (
        "question",
        "Continue the campaign?",
        f"Retribution writes the campaign's mission as it stood at {at} and DCS "
        f"Companion loads it on the server. {DROPPED}",
    )


def restore_question(at: str, minute: int) -> tuple[str, str, str]:
    """Continue's dialog while the campaign's mission is followed, the
    restored mission starting `at` (`HH:MM`)."""
    return (
        "question",
        "Restore the campaign's mission?",
        f"The campaign's mission is running. The restored mission starts at "
        f"{at}, minute {minute} of the running one. From now on only the "
        "restored mission is followed; nothing that happens in the running one "
        "is applied. DCS Companion loads it on the server; players go back to "
        "slot selection.",
    )


def loading(file: str) -> str:
    return f"Companion: loading {file} on the server…"


def not_loaded(file: str) -> str:
    return f"Companion: {file} was not loaded on the server; Continue to try again"


def missions_folder() -> Path:
    return persistency.mission_path_for("x.miz").parent


def dated_files() -> set[str]:
    return {p.name for p in missions_folder().iterdir() if DATED.fullmatch(p.name)}


def carries_marker(path: Path, marker: str) -> bool:
    """Whether the written mission holds the marker static `marker`."""
    with zipfile.ZipFile(path) as miz:
        return (MARKER_PREFIX + marker) in miz.read("mission").decode("utf-8")


def messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records]


class Clock:
    """The wait's clock, moved by the test."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def mission_of(
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    commands: FakeCommands,
    saved_games: Path,
) -> Iterator[Callable[[Window], CampaignMission]]:
    """Begin and Continue on the window's game, model and link, as the main
    window holds them: told each game update; the loads go to `commands`."""
    monkeypatch.setattr(campaignmission, "WAIT_CHECK_MS", 20)
    use_commands(monkeypatch, commands)
    made: list[CampaignMission] = []

    def make(w: Window) -> CampaignMission:
        if w.panel is None:
            w.top_panel()  # the window's side of each game update
        mission = CampaignMission(
            w.game_model, w.game_model.sim_controller, w.link, clock=clock
        )
        mission.set_game(w.game)
        GameUpdateSignal.get_instance().gameupdated.connect(mission.set_game)
        made.append(mission)
        return mission

    yield make
    for mission in made:
        GameUpdateSignal.get_instance().gameupdated.disconnect(mission.set_game)
        mission.set_game(None)


@pytest.fixture
def read_only_missions() -> Iterator[Callable[[bool], None]]:
    """Makes DCS's `Missions/` folder read only, or writable again."""
    folder = missions_folder()
    mode = folder.stat().st_mode

    def set_read_only(read_only: bool) -> None:
        folder.chmod(0o555 if read_only else mode)

    yield set_read_only
    folder.chmod(mode)


def test_begin_writes_dated_file_and_loads(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    mission_of: Callable[[Window], CampaignMission],
    commands: FakeCommands,
    boxes: Boxes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    game = new_game(dynamic=True, squadrons_start_full=True, campaign_file=CAMPAIGN)
    gate = threading.Event()
    # The server runs another mission until the campaign's is loaded.
    script: list[Item] = [event("state", state(game, 1, own=False)), gate, ""]
    w = window(serve(script), game)
    mission = mission_of(w)
    autosave = persistency.dynamic_autosave_path()
    autosave.unlink(missing_ok=True)

    boxes.answer = OK
    mission.begin()

    assert boxes.shown == [BEGIN]
    assert game.turn == 1
    file = game.live.mission_file
    assert file is not None and DATED.fullmatch(file)
    marker = game.live.marker
    assert marker is not None
    assert carries_marker(persistency.mission_path_for(file), marker)
    saved = persistency.load_game(str(autosave))
    assert saved is not None
    assert saved.live.mission_file == file and saved.live.marker == marker
    assert mission.loading == file
    w.wait_for(lambda: commands.loads == [file])
    assert f"Mission load: asked {file}" in messages(caplog)
    w.wait_for(lambda: w.link.status_text() == loading(file))
    w.wait_for(lambda: game.live.current == ("b1", 1))
    assert mission.loading == file, "the mission replaced is not the one loaded"

    script[2] = event("state", state(game, 1, boot="b2"))
    gate.set()
    w.wait_for(lambda: following(game) and mission.loading is None)
    assert game.live.current == ("b2", 1)
    assert mission.load_failed is None
    assert w.link.status_text() == "DCS Companion time: 1 minute"


def test_begin_cancel_changes_nothing(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    mission_of: Callable[[Window], CampaignMission],
    commands: FakeCommands,
    boxes: Boxes,
) -> None:
    game = new_game(dynamic=True, squadrons_start_full=True, campaign_file=CAMPAIGN)
    w = window(serve([]), game)
    mission = mission_of(w)
    files = dated_files()

    boxes.answer = CANCEL
    mission.begin()
    w.pump()

    assert boxes.shown == [BEGIN]
    assert game.turn == 0 and game.live.marker is None
    assert game.live.mission_file is None
    assert dated_files() == files
    assert commands.loads == []
    assert mission.loading is None and not w.link.active


def test_begin_load_refused_then_continue(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    mission_of: Callable[[Window], CampaignMission],
    commands: FakeCommands,
    boxes: Boxes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review Focus 5: the companion went down between the watch's last answer
    and the click."""
    caplog.set_level(logging.INFO)
    commands.load_refuse = (503, "campaign: hook link down")
    game = new_game(dynamic=True, squadrons_start_full=True, campaign_file=CAMPAIGN)
    w = window(serve([]), game)
    mission = mission_of(w)

    boxes.answer = OK
    mission.begin()
    first = game.live.mission_file
    assert first is not None and persistency.mission_path_for(first).exists()
    begun_marker = game.live.marker
    error = "the companion answered 503: campaign: hook link down"
    w.wait_for(lambda: len(boxes.shown) == 2)
    assert boxes.shown[1] == (
        "warning",
        "Mission not loaded",
        f"{first} was written but DCS Companion did not load it: {error}",
    )
    assert game.turn == 1, "the campaign has begun"
    assert mission.loading is None and mission.load_failed == first
    assert f"Mission load: {first} failed: {error}" in messages(caplog)
    w.wait_for(lambda: w.link.status_text() == not_loaded(first))

    commands.load_refuse = None
    flight = plan_by_hand(game, 2)
    boxes.shown.clear()
    mission.continue_()

    assert boxes.shown == [CONTINUE_NOW]
    second = game.live.mission_file
    assert second is not None and DATED.fullmatch(second) and second != first
    assert game.turn == 1, "Continue passes no turn"
    marker = game.live.marker
    assert marker is not None and marker != begun_marker
    assert carries_marker(persistency.mission_path_for(second), marker)
    assert game.live.written_map is not None
    assert game.live.written_map.flight_units(flight), "the ATO's flights in it"
    w.wait_for(lambda: commands.loads == [first, second])
    assert (mission.loading, mission.load_failed) == (second, None)
    w.wait_for(lambda: w.link.status_text() == loading(second))


def test_continue_from_tick(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    mission_of: Callable[[Window], CampaignMission],
    game: Game,
    commands: FakeCommands,
    boxes: Boxes,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    text = state(game, 3)
    running = replace(parse_snapshot(text), packed=zlib.compress(text.encode()))
    assert game.live.decide(running) is MissionVerdict.OWN
    assert game.live.keep(running)
    loaded = save_and_load(game, tmp_path / "ticked.retribution")
    tick = loaded.live.tick
    assert tick is not None and loaded.live.awaiting_pickup
    marker = loaded.live.marker
    # The server's mission is not seen: nothing is followed.
    w = window(serve([]), loaded)
    mission = mission_of(w)

    boxes.answer = OK
    mission.continue_()

    assert boxes.shown == [continue_from(f"{restore_start(tick):%Y-%m-%d %H:%M}")]
    file = loaded.live.mission_file
    assert file is not None and DATED.fullmatch(file)
    assert loaded.live.marker != marker
    assert carries_marker(persistency.mission_path_for(file), str(loaded.live.marker))
    assert any(
        m.startswith(f"Restore: {missions_folder() / file} written")
        for m in messages(caplog)
    )
    assert loaded.conditions.start_time == restore_start(tick)
    assert loaded.live.verdict_of(("b1", 1)) is MissionVerdict.FOREIGN, "retired"
    assert (loaded.live.awaiting_pickup, mission.loading) == (False, file)
    w.wait_for(lambda: commands.loads == [file])


def test_continue_while_followed_confirms(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    mission_of: Callable[[Window], CampaignMission],
    game: Game,
    commands: FakeCommands,
    boxes: Boxes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The launcher checks only when told to.
    monkeypatch.setattr(flightlauncher, "CHECK_INTERVAL_MS", 3_600_000)
    flight = plan_by_hand(game, 2)
    start = flight.flight_plan.startup_time()  # due from the first snapshot
    restored_at = (start + timedelta(minutes=3)).replace(second=0, microsecond=0)
    autosave = persistency.dynamic_autosave_path()

    w = window(serve([event("state", state(game, 3, start=start))]))
    mission = mission_of(w)
    launcher = FlightLauncher(w.link)
    launcher.set_game(game)
    try:
        w.wait_for(
            lambda: following(game)
            and game.live.cursor == 0
            and game.live.tick is not None
            and launcher.receiving
        )
        assert due_flights(
            game, LiveStateContext.clock().now(), True, set(), set()
        ) == [flight]
        autosave.unlink(missing_ok=True)  # live losses saved the first cursor
        marker = game.live.marker
        followed_status = w.link.status_text()
        assert followed_status == "DCS Companion time: 3 minutes"
        maps = len(w.live_maps())
        files = dated_files()

        boxes.answer = CANCEL
        mission.continue_()
        assert boxes.shown == [restore_question(f"{restored_at:%H:%M}", 3)]
        assert dated_files() == files and not autosave.exists()
        assert game.live.marker == marker and following(game)
        assert w.link.status_text() == followed_status
        assert len(w.live_maps()) == maps
        assert commands.loads == []

        boxes.shown.clear()
        boxes.answer = OK
        mission.continue_()
        assert boxes.shown == [restore_question(f"{restored_at:%H:%M}", 3)]
        file = game.live.mission_file
        assert file is not None and dated_files() == files | {file}
        assert game.live.marker != marker
        saved = persistency.load_game(str(autosave))
        assert saved is not None and saved.live.marker == game.live.marker
        # At once, not at the old mission's next snapshot.
        assert not following(game)
        assert w.link.status_text() == loading(file)
        [cleared] = w.live_maps()[maps:]
        assert cleared.units == () and cleared.boot == ""
        w.wait_for(lambda: commands.loads == [file])

        launcher.check()
        w.pump()
        assert not launcher.launching and not flight.launched
        assert commands.requests == []
    finally:
        launcher.stop()


def test_load_wait_times_out(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    mission_of: Callable[[Window], CampaignMission],
    game: Game,
    commands: FakeCommands,
    boxes: Boxes,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    w = window(serve([]))
    mission = mission_of(w)

    boxes.answer = OK
    mission.continue_()
    file = game.live.mission_file
    assert file is not None
    w.wait_for(lambda: commands.loads == [file])
    w.wait_for(lambda: w.link.status_text() == loading(file))
    clock.now += 179.0
    w.pump()
    assert mission.loading == file

    clock.now += 2.0
    w.wait_for(lambda: mission.loading is None)
    assert mission.load_failed == file
    assert w.link.status_text() == not_loaded(file)
    assert f"Mission load: {file} not loaded within 180 s" in messages(caplog)
    assert [b[1] for b in boxes.shown] == ["Continue the campaign?"]

    # Continue tries again.
    mission.continue_()
    second = game.live.mission_file
    assert second is not None and second != file
    w.wait_for(lambda: commands.loads == [file, second])
    assert mission.loading == second and mission.load_failed is None


def test_wait_ends_on_another_mission(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    mission_of: Callable[[Window], CampaignMission],
    game: Game,
    commands: FakeCommands,
    boxes: Boxes,
) -> None:
    gate = threading.Event()
    script: list[Item] = [gate, ""]
    w = window(serve(script))
    mission = mission_of(w)

    boxes.answer = OK
    mission.continue_()
    file = game.live.mission_file
    assert file is not None
    w.wait_for(lambda: commands.loads == [file])

    # DCS loaded something else.
    script[1] = event("state", state(game, 1, boot="b2", own=False))
    gate.set()
    w.wait_for(lambda: mission.loading is None)
    assert mission.load_failed is None
    assert w.link.status_text() == FOLLOWED_NOT
    assert not following(game)


def test_failed_load_cleared_once_followed(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    mission_of: Callable[[Window], CampaignMission],
    game: Game,
    commands: FakeCommands,
    boxes: Boxes,
) -> None:
    """The campaign's mission loaded after all (by hand, or late): the status
    no longer says it was not loaded."""
    commands.load_refuse = (504, "campaign: the hook did not answer in time")
    gate = threading.Event()
    script: list[Item] = [gate, ""]
    w = window(serve(script))
    mission = mission_of(w)

    boxes.answer = OK
    mission.continue_()
    file = game.live.mission_file
    assert file is not None
    w.wait_for(lambda: mission.load_failed == file)
    w.wait_for(lambda: w.link.status_text() == not_loaded(file))

    script[1] = event("state", state(game, 1, boot="b2"))
    gate.set()
    w.wait_for(lambda: following(game) and mission.load_failed is None)
    assert w.link.status_text() == "DCS Companion time: 1 minute"


def test_failed_write_changes_nothing(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    mission_of: Callable[[Window], CampaignMission],
    game: Game,
    commands: FakeCommands,
    boxes: Boxes,
    read_only_missions: Callable[[bool], None],
) -> None:
    autosave = persistency.dynamic_autosave_path()
    w = window(serve([event("state", state(game, 3))]))
    mission = mission_of(w)
    w.wait_for(
        lambda: following(game) and game.live.cursor == 0 and game.live.tick is not None
    )
    autosave.unlink(missing_ok=True)  # live losses saved the first cursor
    marker, tick = game.live.marker, game.live.tick
    verdicts = dict(game.live.verdicts)
    start_time = game.conditions.start_time
    status = w.link.status_text()
    maps = len(w.live_maps())
    files = dated_files()

    read_only_missions(True)
    boxes.answer = OK
    mission.continue_()
    read_only_missions(False)

    [_, (method, title, text)] = boxes.shown
    assert (method, title) == ("warning", "Mission not written")
    assert re.fullmatch(
        rf"{DATED.pattern} was not written: \[Errno 13\] Permission denied: .*", text
    )
    assert game.live.marker == marker and game.live.tick is tick
    assert game.live.verdicts == verdicts
    assert game.live.mission_file is None
    assert game.conditions.start_time == start_time
    assert following(game) and not autosave.exists()
    assert dated_files() == files
    w.pump()
    assert commands.loads == []
    assert mission.loading is None and mission.load_failed is None
    assert w.link.status_text() == status
    assert len(w.live_maps()) == maps


def test_begin_write_failed_then_continue(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    mission_of: Callable[[Window], CampaignMission],
    commands: FakeCommands,
    boxes: Boxes,
    read_only_missions: Callable[[bool], None],
) -> None:
    """Begin's failed write leaves the campaign begun, with nothing written:
    Continue writes it."""
    game = new_game(dynamic=True, squadrons_start_full=True, campaign_file=CAMPAIGN)
    w = window(serve([]), game)
    mission = mission_of(w)

    read_only_missions(True)
    boxes.answer = OK
    mission.begin()
    read_only_missions(False)

    [begin, (method, title, text)] = boxes.shown
    assert begin == BEGIN
    assert (method, title) == ("warning", "Mission not written")
    assert re.fullmatch(rf"{DATED.pattern} was not written: .*", text)
    assert game.turn == 1
    assert (game.live.marker, game.live.mission_file) == (None, None)
    w.pump()
    assert commands.loads == [] and mission.loading is None
    assert w.link.active

    boxes.shown.clear()
    mission.continue_()
    assert boxes.shown == [CONTINUE_NOW]
    file, marker = game.live.mission_file, game.live.marker
    assert file is not None and marker is not None
    assert carries_marker(persistency.mission_path_for(file), marker)
    w.wait_for(lambda: commands.loads == [file])


def test_spawn_confirmed_into_restored_away_mission(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    mission_of: Callable[[Window], CampaignMission],
    game: Game,
    commands: FakeCommands,
    boxes: Boxes,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A spawn sent into the campaign's mission, confirmed after Continue
    retired it while the link was stale, is refused: its flight launches
    again into the restored mission."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(companionlinkcontroller, "STALE_S", 1.0)
    monkeypatch.setattr(companionlinkcontroller, "STALE_CHECK_MS", 50)
    commands.statuses = ["waiting"]  # accepted, not confirmed yet
    flight = plan_by_hand(game, 2)
    start = flight.flight_plan.startup_time()  # due from the first snapshot

    gate = threading.Event()
    # The restored mission's snapshot carries the marker Continue makes.
    script: list[Item] = [event("state", state(game, 1, start=start)), gate, ""]
    w = window(serve(script))
    mission = mission_of(w)
    launcher = FlightLauncher(w.link)
    launcher.confirm_warn_s = launcher.confirm_cap_s = 30.0
    launcher.confirm_poll_s = 0.05
    ended: list[tuple[object, str]] = []
    launcher.launch_failed.connect(lambda f, message: ended.append((f, message)))
    launcher.set_game(game)
    try:
        w.wait_for(lambda: following(game) and launcher.receiving)
        launcher.launch_now(flight)
        w.wait_for(lambda: len(commands.requests) == 1)
        w.wait_for(
            lambda: w.link.status_text()
            == "Companion: waiting for a mission on the server"
        )
        assert launcher.is_busy(flight)

        boxes.answer = OK
        mission.continue_()
        assert [b[1] for b in boxes.shown] == ["Continue the campaign?"]
        with commands.lock:
            commands.statuses = ["spawned"]
        w.wait_for(lambda: not launcher.is_busy(flight))

        assert flight.launched is False
        assert flight.live_group_name is None
        said = (
            f"{flight} spawned after the campaign's mission stopped being "
            "followed; it launches again"
        )
        assert f"Launch: {said}" in messages(caplog)
        assert ended == [(flight, said)]

        with commands.lock:
            commands.statuses = ["waiting"]
        script[2] = event("state", state(game, 1, boot="b2", start=start))
        gate.set()
        w.wait_for(lambda: len(commands.requests) == 2)
        assert mission.loading is None, "the restored mission came"
        names = [u["name"] for u in commands.requests[1][0]["group"]["units"].values()]
        assert game.live.name_map.flight_units(flight) == names
        with commands.lock:
            commands.statuses = ["spawned"]
        w.wait_for(lambda: flight.launched)
    finally:
        launcher.stop()


def test_dynamic_campaign_panel_has_no_begin_or_restore(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    saved_games: Path,
) -> None:
    game = new_game(dynamic=True, squadrons_start_full=True, campaign_file=CAMPAIGN)
    w = window(serve([]), game)
    panel = w.top_panel()
    assert not hasattr(panel, "restore_button")
    for button in (panel.passTurnButton, panel.proceedButton):
        assert not button.isVisibleTo(panel), button.text()
    assert not panel.max_players.isVisibleTo(panel), "no client slots to count"


def test_normal_campaign_panel_unchanged(
    serve: Callable[..., FakeCompanion],
    window: Callable[..., Window],
    saved_games: Path,
) -> None:
    game = new_game(dynamic=False, squadrons_start_full=True, campaign_file=CAMPAIGN)
    w = window(serve([]), game)
    panel = w.top_panel()
    assert panel.passTurnButton.text() == "Begin Campaign"
    assert panel.passTurnButton.isVisibleTo(panel)
    assert panel.proceedButton.text() == "Take off"
    assert panel.proceedButton.isVisibleTo(panel)
    assert not panel.proceedButton.isEnabled(), "not before Begin Campaign"

    game.turn = 1
    GameUpdateSignal.get_instance().updateGame(game)
    assert panel.passTurnButton.text() == "Pass Turn"
    assert panel.passTurnButton.isVisibleTo(panel)
    assert panel.proceedButton.isVisibleTo(panel)
    assert not w.link.active
