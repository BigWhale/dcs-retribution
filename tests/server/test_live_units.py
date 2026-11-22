import asyncio
import threading
from asyncio import Queue
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from dcs.terrain import PersianGulf

from game import persistency
from game.companionlink.snapshot import LiveSnapshot, LiveUnit
from game.server.eventstream.eventstream import EventStream
from game.server.eventstream import models as eventstream_models
from game.server.eventstream.models import GameUpdateEventsJs
from game.server.liveunits.models import LiveUnitJs, live_unit_sidc
from game.sidc import (
    AirEntity,
    LandUnitEntity,
    StandardIdentity,
    SymbolSet,
)
from game.sim import GameUpdateEvents


@pytest.fixture(autouse=True)
def _persistency(tmp_path: Path) -> None:
    # The aircraft type table loads data files that reach for the saved-games
    # folder, as it does in the app; point it at a throwaway dir.
    persistency.setup(str(tmp_path), prefer_liberation_payloads=False, port=16884)


def _unit(category: str, type_name: str, side: int) -> LiveUnit:
    centre = PersianGulf().map_view_default.position
    return LiveUnit(
        name="u",
        group="g",
        category=category,
        type=type_name,
        side=side,
        x=centre.x,
        y=centre.y,
        alt=0.0,
        heading=90.0,
        speed=10.0,
        in_air=category in ("plane", "helicopter"),
        player=None,
    )


def test_all_for_places_units_on_the_map() -> None:
    terrain = PersianGulf()
    centre = terrain.map_view_default.position
    unit = LiveUnit(
        name="Uzi 1-1",
        group="Uzi 1",
        category="plane",
        type="F-16C_50",
        side=2,
        x=centre.x,
        y=centre.y,
        alt=3000.0,
        heading=270.0,
        speed=200.0,
        in_air=True,
        player="Bob",
    )
    snapshot = LiveSnapshot(
        minute=12, boot="b", session=1, model_time=720.0, units=(unit,), size=0
    )
    theater: Any = SimpleNamespace(terrain=terrain)

    [js] = LiveUnitJs.all_for(snapshot, theater)

    assert 24 < js.position.lat < 30 and 50 < js.position.lng < 60
    assert (js.name, js.side, js.altitude, js.in_air, js.player) == (
        "Uzi 1-1",
        2,
        3000.0,
        True,
        "Bob",
    )
    assert LiveUnitJs.all_for(None, theater) == []


def test_a_snapshot_that_does_not_convert_is_skipped(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fresh warn-once memory, so the warning shows whatever ran before.
    monkeypatch.setattr(eventstream_models, "_warned_live_unit_errors", set())
    terrain = PersianGulf()
    centre = terrain.map_view_default.position
    bad: Any = None  # a unit name the map model rejects
    unit = LiveUnit(
        name=bad,
        group="Uzi 1",
        category="plane",
        type="F-16C_50",
        side=2,
        x=centre.x,
        y=centre.y,
        alt=3000.0,
        heading=270.0,
        speed=200.0,
        in_air=True,
        player="Bob",
    )
    snapshot = LiveSnapshot(
        minute=12, boot="b", session=1, model_time=720.0, units=(unit,), size=0
    )
    game: Any = SimpleNamespace(
        theater=SimpleNamespace(terrain=terrain), get_culling_zones=lambda: []
    )
    events = GameUpdateEvents().begin_new_turn().update_live_units(snapshot)

    js = GameUpdateEventsJs.from_events(events, game)

    assert js.live_units is None
    assert js.new_turn is True
    assert any("did not convert" in r.getMessage() for r in caplog.records)


def test_put_nowait_from_another_thread_reaches_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(EventStream, "_queue", Queue())
    monkeypatch.setattr(EventStream, "_loop", None, raising=False)
    monkeypatch.setattr(EventStream, "_loop_thread", None, raising=False)
    loop = asyncio.new_event_loop()
    waiting = threading.Event()
    got: list[GameUpdateEvents] = []

    async def serve() -> None:
        task = asyncio.ensure_future(EventStream.get())
        await asyncio.sleep(0)  # get has recorded the loop and waits
        waiting.set()
        got.append(await asyncio.wait_for(task, 10))

    server = threading.Thread(target=loop.run_until_complete, args=(serve(),))
    server.start()
    assert waiting.wait(5)
    events = GameUpdateEvents().begin_new_turn()
    EventStream.put_nowait(events)
    # The put wakes the loop at once; a put that does not (the queue touched
    # from this thread) waits for the loop's next timer, wait_for's 10 s.
    server.join(2)
    arrived = list(got)
    server.join(10)
    loop.close()

    assert len(arrived) == 1 and arrived[0] is events
    # After shutdown (the loop closed) a put is dropped, not raised.
    EventStream.put_nowait(GameUpdateEvents().shut_down())


def test_sidc_affiliation_and_dimension() -> None:
    jet = live_unit_sidc(_unit("plane", "F-16C_50", 2))
    assert jet.standard_identity == StandardIdentity.FRIEND
    assert jet.symbol_set == SymbolSet.AIR
    assert jet.entity == AirEntity.FIGHTER

    hind = live_unit_sidc(_unit("helicopter", "Mi-24P", 1))
    assert hind.standard_identity == StandardIdentity.HOSTILE_FAKER
    assert (hind.symbol_set, hind.entity) == (SymbolSet.AIR, AirEntity.ROTARY_WING)

    ship = live_unit_sidc(_unit("ship", "Dry-cargo ship-1", 0))
    assert ship.standard_identity == StandardIdentity.UNKNOWN
    assert ship.symbol_set == SymbolSet.SEA_SURFACE

    tank = live_unit_sidc(_unit("ground", "T-72B", 1))
    assert tank.standard_identity == StandardIdentity.HOSTILE_FAKER
    assert (tank.symbol_set, tank.entity) == (
        SymbolSet.LAND_UNIT,
        LandUnitEntity.ARMOR_ARMORED_MECHANIZED_SELF_PROPELLED_TRACKED,
    )


def test_sidc_in_json() -> None:
    unit = _unit("plane", "F-16C_50", 2)
    snapshot = LiveSnapshot(
        minute=1, boot="b", session=1, model_time=60.0, units=(unit,), size=0
    )
    theater: Any = SimpleNamespace(terrain=PersianGulf())

    [js] = LiveUnitJs.all_for(snapshot, theater)

    assert len(js.sidc) == 20 and js.sidc.isdigit()
    assert js.sidc == str(live_unit_sidc(unit))


def test_unknown_type_is_generic() -> None:
    sidc = live_unit_sidc(_unit("plane", "NoSuchJet", 2))
    assert (sidc.symbol_set, sidc.entity) == (SymbolSet.AIR, AirEntity.UNSPECIFIED)
