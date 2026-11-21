import asyncio
import threading
from asyncio import Queue
from types import SimpleNamespace
from typing import Any

import pytest
from dcs.terrain import PersianGulf

from game.companionlink.snapshot import LiveSnapshot, LiveUnit
from game.server.eventstream.eventstream import EventStream
from game.server.liveunits.models import LiveUnitJs
from game.sim import GameUpdateEvents


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
