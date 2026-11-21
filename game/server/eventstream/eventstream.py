from __future__ import annotations

import asyncio
import logging
import threading
from asyncio import AbstractEventLoop, Queue
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Optional

from game.sim import GameUpdateEvents


class EventStream:
    """The map's update events, from any thread to the server's event loop.

    The queue belongs to the server's asyncio loop (uvicorn's thread), which is
    recorded on the first `get`. A put or drain from another thread (the GUI, the
    sim, a route's worker) is handed to that loop with `call_soon_threadsafe`, so
    the queue is only ever touched on the loop's thread, in the order the calls
    were made. Before the loop is known, and on the loop's own thread, they act
    directly. Once the loop is closed (shutdown), events are dropped.
    """

    _queue: Queue[GameUpdateEvents] = Queue()
    _loop: Optional[AbstractEventLoop] = None
    _loop_thread: Optional[int] = None

    @classmethod
    def _on_loop(cls, fn: Callable[..., object], *args: object) -> bool:
        """Runs fn(*args) on the server's loop if called from another thread.

        True when the call was handed over (or dropped because the loop is
        closed); False when the caller should run it directly.
        """
        loop = cls._loop
        if loop is None or threading.get_ident() == cls._loop_thread:
            return False
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            # The loop is closed: the server has shut down and nobody reads.
            logging.debug("EventStream: the server's loop is closed; event dropped")
        return True

    @classmethod
    def drain(cls) -> None:
        if cls._on_loop(cls._drain):
            return
        cls._drain()

    @classmethod
    def _drain(cls) -> None:
        while not cls._queue.empty():
            cls._queue.get_nowait()
            cls._queue.task_done()

    @classmethod
    async def put(cls, events: GameUpdateEvents) -> None:
        await cls._queue.put(events)

    @classmethod
    def put_nowait(cls, events: GameUpdateEvents) -> None:
        # The queue has infinite size so this should never need to block anyway. If for
        # some reason the queue is full this will throw QueueFull.
        if cls._on_loop(cls._queue.put_nowait, events):
            return
        cls._queue.put_nowait(events)

    @classmethod
    async def get(cls) -> GameUpdateEvents:
        cls._loop = asyncio.get_running_loop()
        cls._loop_thread = threading.get_ident()
        events = await cls._queue.get()
        cls._queue.task_done()
        return events

    @staticmethod
    @contextmanager
    def event_context() -> Iterator[GameUpdateEvents]:
        events = GameUpdateEvents()
        yield events
        EventStream.put_nowait(events)
