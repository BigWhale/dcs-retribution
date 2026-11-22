"""The link to DCS Companion's snapshot stream, against a fake companion.

The fake serves `GET /v1/hello`, `GET /v1/stream` and `GET /v1/events` from a
real HTTP server on 127.0.0.1, one script per stream connection, with the
field names of the companion's minute snapshots (the state spike's sample)
and of its event log.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator, Optional, Union
from urllib.parse import parse_qs, urlsplit

import pytest

from game.companionlink.eventlog import EventBatch, LogPos, LogRef, parse_events_page
from game.companionlink.link import CompanionLink, LinkStatus
from game.companionlink.snapshot import LiveSnapshot

TOKEN = "tok-1"
DEADLINE = 3.0

#: Ends the stream connection that reads it.
CLOSE = object()

#: What a stream connection sends: event text, CLOSE, or an Event to wait for.
Item = Union[str, object, threading.Event]


class FakeCompanion:
    """The companion's data port: hello, the stream (one script per stream
    connection) and the event log's `entries`, recording each `after` and
    `limit` asked."""

    def __init__(self, scripts: Optional[list[list[Item]]] = None) -> None:
        self.scripts: list[list[Item]] = scripts or []
        self.entries: list[dict[str, Any]] = []
        self.afters: list[int] = []
        self.limits: list[int] = []
        self.closing = threading.Event()
        self.lock = threading.Lock()
        self.streams = 0
        self.refused = 0
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self, port: int = 0) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    with fake.lock:
                        fake.refused += 1
                    self.answer(401, {"error": "unauthorized"})
                    return
                url = urlsplit(self.path)
                if url.path == "/v1/hello":
                    self.answer(
                        200,
                        {
                            "protocol": 1,
                            "boot": "b1",
                            "session": 1,
                            "modelTime": 5,
                            "lastSeq": fake.last_seq(),
                        },
                    )
                elif url.path == "/v1/events":
                    query = parse_qs(url.query)
                    after = int(query.get("after", ["0"])[0])
                    limit = int(query.get("limit", ["500"])[0])
                    with fake.lock:
                        fake.afters.append(after)
                        fake.limits.append(limit)
                        page = [e for e in fake.entries if e["seq"] > after][:limit]
                    self.answer(
                        200,
                        {
                            "boot": "b1",
                            "session": 1,
                            "modelTime": 5,
                            "lastSeq": fake.last_seq(),
                            "entries": page,
                        },
                    )
                elif url.path == "/v1/stream":
                    with fake.lock:
                        n = fake.streams
                        fake.streams += 1
                    script = fake.scripts[n] if n < len(fake.scripts) else []
                    self.stream(script)
                else:
                    self.answer(404, {"error": "not found"})

            def answer(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                if status == 401:
                    self.send_header("WWW-Authenticate", "Bearer")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def stream(self, script: list[Item]) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for item in script:
                        if item is CLOSE:
                            return
                        if isinstance(item, threading.Event):
                            while not item.wait(0.02):
                                if fake.closing.is_set():
                                    return
                            continue
                        assert isinstance(item, str)
                        self.wfile.write(item.encode())
                        self.wfile.flush()
                    fake.closing.wait()
                except OSError:
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server.daemon_threads = False
        self.server = server
        self.thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    def last_seq(self) -> int:
        with self.lock:
            return self.entries[-1]["seq"] if self.entries else 0

    def add(self, *seqs: int) -> None:
        with self.lock:
            self.entries.extend(entry(n) for n in seqs)

    @property
    def address(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.closing.set()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join()


class Recorder:
    """The link's callbacks, as they came."""

    def __init__(self) -> None:
        self.snapshots: list[LiveSnapshot] = []
        self.statuses: list[LinkStatus] = []
        self.batches: list[EventBatch] = []

    def on_snapshot(self, snap: LiveSnapshot) -> None:
        self.snapshots.append(snap)

    def on_events(self, batch: EventBatch) -> None:
        self.batches.append(batch)

    def seqs(self) -> list[int]:
        """Every delivered entry's seq, in order."""
        return [e.seq for b in self.batches for e in b.entries]

    def on_status(self, status: LinkStatus) -> None:
        self.statuses.append(status)


def wait_for(cond: Callable[[], bool], deadline: float = DEADLINE) -> None:
    end = time.monotonic() + deadline
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.01)


def event(kind: str, data: str) -> str:
    return f"event: {kind}\ndata: {data}\n\n"


def entry(seq: int, kind: str = "dead", **extra: Any) -> dict[str, Any]:
    e: dict[str, Any] = {
        "seq": seq,
        "kind": kind,
        "boot": "b1",
        "session": 1,
        "modelTime": 2334.5 + seq,
        "time": "2026-11-21T19:40:12.345Z",
        "hookN": seq,
        "unit": {"name": f"Tank-{seq}", "type": "T-55", "side": 1},
        "pos": {"x": 100.0, "y": 200.0, "alt": 12.3, "lat": 26.1, "lon": 56.2},
    }
    e.update(extra)
    return e


def unit(name: str, x: float = 100.0, y: float = 200.0, **extra: Any) -> dict[str, Any]:
    u: dict[str, Any] = {
        "name": name,
        "id": abs(hash(name)) % 100000,
        "type": extra.pop("type", "T-55"),
        "x": x,
        "y": y,
        "alt": 12.3,
        "hdg": 90,
        "spd": 0,
        "vs": 0,
        "life": 1,
        "life0": 1,
        "active": True,
    }
    u.update(extra)
    return u


def group(
    name: str, cat: int, side: int, units: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "name": name,
        "cat": cat,
        "id": abs(hash(name)) % 100000,
        "side": side,
        "task": False,
        "units": units,
    }


def state(
    minute: int,
    *groups: dict[str, Any],
    statics: Optional[list[dict[str, Any]]] = None,
    meta: Optional[dict[str, Any]] = None,
) -> str:
    return json.dumps(
        {
            "minute": minute,
            "boot": "b1",
            "session": 1,
            "modelTime": 2334.662 + 60 * minute,
            "takenAt": "2026-11-21T19:40:12.345Z",
            "state": {
                "meta": {
                    "version": 1,
                    "modelTime": 2334.662 + 60 * minute,
                    **(meta or {}),
                },
                "groups": list(groups),
                "statics": statics or [],
                "airbases": [],
            },
        },
        separators=(",", ":"),
    )


def tank(minute: int, name: str = "Tank") -> str:
    return state(minute, group(name, 2, 1, [unit(f"{name}-1")]))


@pytest.fixture
def fakes() -> Iterator[list[FakeCompanion]]:
    made: list[FakeCompanion] = []
    yield made
    for fake in made:
        fake.close()


@pytest.fixture
def links() -> Iterator[list[CompanionLink]]:
    made: list[CompanionLink] = []
    yield made
    for link in made:
        link.stop()


@pytest.fixture
def serve(
    fakes: list[FakeCompanion],
) -> Callable[..., FakeCompanion]:
    def start(*scripts: list[Item], port: int = 0) -> FakeCompanion:
        fake = FakeCompanion(list(scripts))
        fake.start(port)
        fakes.append(fake)
        return fake

    return start


@pytest.fixture
def connect(
    links: list[CompanionLink],
) -> Callable[..., tuple[CompanionLink, Recorder]]:
    def start(
        address: str, token: str = TOKEN, events: bool = False, **kw: Any
    ) -> tuple[CompanionLink, Recorder]:
        rec = Recorder()
        if events:
            kw["on_events"] = rec.on_events
        link = CompanionLink(address, token, rec.on_snapshot, rec.on_status, **kw)
        links.append(link)
        link.start()
        return link, rec

    return start


def test_snapshot_parsed(serve: Any, connect: Any) -> None:
    plane = group(
        "Uzi",
        0,
        2,
        [
            unit(
                "Uzi 1-1",
                x=382039.1,
                y=-352647.4,
                type="FA-18C_hornet",
                player="Uzi 11",
                inAir=True,
                hdg=207.4,
                spd=220,
                alt=3048.0,
                fuel=0.8,
                callsign="Uzi11",
                id=12,
            )
        ],
    )
    plane["id"] = 7
    armor = group("Armor", 2, 1, [unit("Armor-1", id=40), unit("Armor-2", id=41)])
    armor["id"] = 31
    boat = group("Boat", 3, 0, [unit("Boat-1", type="speedboat", id=50)])
    boat["id"] = 9
    statics = [
        {
            "name": "0116 | Tech combine object",
            "id": 116,
            "side": 1,
            "exists": True,
            "type": "Tech combine",
            "x": 168999.8,
            "y": -188324,
            "alt": 812.4,
            "hdg": 90,
            "life": 1000,
        },
        {"name": "0117 | Tech hangar A object", "id": 117, "side": 1, "exists": False},
    ]
    fake = serve(
        [
            ": keep-alive\n\n",
            event(
                "state",
                state(
                    0,
                    plane,
                    armor,
                    boat,
                    statics=statics,
                    meta={
                        "absTime": 30000.5,
                        "date": {"Year": 2005, "Month": 7, "Day": 1},
                    },
                ),
            ),
        ]
    )
    _, rec = connect(fake.address)
    wait_for(lambda: len(rec.snapshots) == 1)

    snap = rec.snapshots[0]
    assert snap.minute == 0
    assert snap.boot == "b1"
    assert snap.session == 1
    assert snap.model_time == pytest.approx(2334.662)
    assert len(snap.units) == 5
    assert snap.unit_count == 4
    assert snap.abs_time == datetime(2005, 7, 1, 8, 20, 0, 500000)
    assert snap.max_group_id == 31
    assert snap.max_unit_id == 117  # statics count, gone ones too
    assert {"Uzi", "Uzi 1-1", "0116 | Tech combine object"} <= snap.names
    assert "0117 | Tech hangar A object" in snap.names

    by_name = {u.name: u for u in snap.units}
    jet = by_name["Uzi 1-1"]
    assert jet.category == "plane"
    assert jet.group == "Uzi"
    assert jet.type == "FA-18C_hornet"
    assert jet.side == 2
    assert jet.player == "Uzi 11"
    assert jet.in_air is True
    assert jet.heading == 207.4
    assert jet.speed == 220
    assert jet.x == 382039.1
    assert jet.y == -352647.4
    assert jet.alt == 3048.0

    assert by_name["Armor-1"].category == "ground"
    assert by_name["Armor-1"].side == 1
    assert by_name["Armor-1"].in_air is False
    assert by_name["Armor-1"].player is None
    assert by_name["Boat-1"].category == "ship"
    assert by_name["Boat-1"].side == 0

    combine = by_name["0116 | Tech combine object"]
    assert combine.category == "static"
    assert combine.group == ""
    assert combine.type == "Tech combine"
    assert combine.side == 1
    assert "0117 | Tech hangar A object" not in by_name
    assert snap.units[-1] is combine

    wait_for(lambda: rec.statuses[-2:] == [LinkStatus.LINKED, LinkStatus.RECEIVING])


def test_reconnects_after_drop(serve: Any, connect: Any) -> None:
    fake = serve([event("state", tank(0)), CLOSE], [event("state", tank(1))])
    _, rec = connect(fake.address, retry=0.1)
    wait_for(lambda: len(rec.snapshots) == 2)
    assert [s.minute for s in rec.snapshots] == [0, 1]


def test_refused(serve: Any, connect: Any) -> None:
    fake = serve([event("state", tank(0))])
    _, rec = connect(fake.address, token="bad", retry=0.1)
    wait_for(lambda: fake.refused >= 2)
    assert rec.statuses == [LinkStatus.REFUSED]
    assert rec.snapshots == []
    assert fake.streams == 0


def test_waits_for_server(serve: Any, connect: Any) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    _, rec = connect(f"http://127.0.0.1:{port}", retry=0.1)
    wait_for(lambda: LinkStatus.WAITING in rec.statuses)
    assert rec.snapshots == []

    serve([event("state", tank(0))], port=port)
    wait_for(lambda: len(rec.snapshots) == 1)
    assert rec.statuses[-2:] == [LinkStatus.LINKED, LinkStatus.RECEIVING]


def test_silent_stream_reconnects(serve: Any, connect: Any) -> None:
    fake = serve()  # every stream: headers, then nothing
    _, rec = connect(fake.address, retry=0.1, read_timeout=0.3)
    wait_for(lambda: fake.streams >= 2)
    assert rec.snapshots == []


def test_large_snapshot(serve: Any, connect: Any) -> None:
    units = [unit(f"Armor-{i}", x=1000.0 + i, y=-2000.0 - i) for i in range(6000)]
    data = state(0, group("Armor", 2, 1, units))
    assert len(data) > 500_000
    fake = serve([event("state", data)])
    _, rec = connect(fake.address)
    wait_for(lambda: len(rec.snapshots) == 1)
    snap = rec.snapshots[0]
    assert len(snap.units) == 6000
    assert snap.unit_count == 6000
    assert snap.size > 500_000
    assert snap.size == len(data.encode())


def test_bad_unit_skipped_bad_event_survives(
    serve: Any, connect: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="game.companionlink")
    broken = unit("Armor-2")
    del broken["x"]
    good = state(0, group("Armor", 2, 1, [unit("Armor-1"), broken, unit("Armor-3")]))
    fake = serve(
        [
            event("state", "{not json"),
            event("events", '{"lastSeq":4}'),
            event("state", good),
        ]
    )
    _, rec = connect(fake.address)
    wait_for(lambda: len(rec.snapshots) == 1)
    assert [u.name for u in rec.snapshots[0].units] == ["Armor-1", "Armor-3"]
    assert fake.streams == 1

    warnings = [
        r
        for r in caplog.records
        if r.name == "game.companionlink" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert TOKEN not in caplog.text


def test_stop(serve: Any, connect: Any) -> None:
    release = threading.Event()
    fake = serve([event("state", tank(0)), release, event("state", tank(1))])
    link, rec = connect(fake.address, retry=0.1)
    wait_for(lambda: len(rec.snapshots) == 1)

    started = time.monotonic()
    link.stop()
    assert time.monotonic() - started < 2.0
    assert not link.is_alive()

    seen = (len(rec.snapshots), len(rec.statuses))
    release.set()
    time.sleep(0.3)
    assert (len(rec.snapshots), len(rec.statuses)) == seen


def test_events_read_after_hello_and_on_notice(serve: Any, connect: Any) -> None:
    notice = threading.Event()
    fake = serve([notice, event("events", '{"lastSeq":6}')])
    fake.add(1, 2, 3, 4)
    _, rec = connect(fake.address, events_after=2, events=True)
    wait_for(lambda: len(rec.batches) == 1)
    fake.add(5, 6)
    notice.set()
    wait_for(lambda: len(rec.batches) == 2)

    assert [[e.seq for e in b.entries] for b in rec.batches] == [[3, 4], [5, 6]]
    assert [b.through for b in rec.batches] == [4, 6]
    assert fake.afters == [2, 4]


def test_events_paged(serve: Any, connect: Any) -> None:
    fake = serve()
    fake.add(*range(1, 12001))
    _, rec = connect(fake.address, events_after=0, events=True)
    wait_for(lambda: len(rec.batches) == 3)

    assert fake.afters == [0, 5000, 10000]
    assert fake.limits == [5000, 5000, 5000]
    assert rec.seqs() == list(range(1, 12001))
    assert [b.through for b in rec.batches] == [5000, 10000, 12000]


def test_events_new_cursor_starts_at_hello(serve: Any, connect: Any) -> None:
    fake = serve()
    fake.add(*range(1, 41))
    _, rec = connect(fake.address, events_after=None, events=True)
    wait_for(lambda: len(rec.batches) == 1)
    wait_for(lambda: LinkStatus.LINKED in rec.statuses)

    assert rec.batches[0].entries == ()
    assert rec.batches[0].through == 40
    assert all(after >= 40 for after in fake.afters)


def test_events_reconnect_misses_nothing(serve: Any, connect: Any) -> None:
    drop = threading.Event()
    fake = serve([drop, CLOSE], [])
    fake.add(1, 2, 3)
    _, rec = connect(fake.address, retry=0.1, events_after=0, events=True)
    wait_for(lambda: rec.seqs() == [1, 2, 3])
    fake.add(4)
    drop.set()
    wait_for(lambda: fake.streams == 2 and len(rec.batches) == 2)

    assert rec.seqs() == [1, 2, 3, 4]
    assert fake.afters == [0, 3]


def test_bad_entry_skipped(
    serve: Any, connect: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="game.companionlink")
    fake = serve()
    fake.add(1, 2, 3)
    del fake.entries[1]["kind"]
    _, rec = connect(fake.address, events_after=0, events=True)
    wait_for(lambda: len(rec.batches) == 1)
    wait_for(lambda: LinkStatus.LINKED in rec.statuses)

    assert rec.seqs() == [1, 3]
    warnings = [
        r
        for r in caplog.records
        if r.name == "game.companionlink" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1


def test_parse_events_page() -> None:
    full = entry(
        7,
        "kill",
        session=2,
        unit={
            "name": "Uzi 1-1",
            "type": "FA-18C_hornet",
            "side": 2,
            "category": "plane",
            "group": "Uzi",
            "player": "Uzi 11",
        },
        other={"name": "123456", "category": "scenery", "side": 0},
        weapon="GBU-12",
        place="Bandar Abbas",
        side=1,
        group="Armor",
        pos={"x": 382039.1, "y": -352647.4, "alt": 812.4, "lat": 26.1, "lon": 56.2},
    )
    page = parse_events_page(
        json.dumps(
            {
                "boot": "b1",
                "session": 2,
                "modelTime": 2401.2,
                "lastSeq": 9,
                "entries": [full, {"seq": 8}, "junk"],
            }
        )
    )

    assert page.last_seq == 9
    assert page.skipped == 2
    assert len(page.entries) == 1
    e = page.entries[0]
    assert e.seq == 7
    assert e.kind == "kill"
    assert e.boot == "b1"
    assert e.session == 2
    assert e.mission == ("b1", 2)
    assert e.unit == LogRef(
        name="Uzi 1-1",
        type="FA-18C_hornet",
        side=2,
        category="plane",
        group="Uzi",
        player="Uzi 11",
    )
    assert e.other == LogRef(name="123456", category="scenery", side=0)
    assert e.place == "Bandar Abbas"
    assert e.side == 1
    assert e.group == "Armor"
    assert e.pos == LogPos(x=382039.1, y=-352647.4, alt=812.4)

    with pytest.raises(ValueError):
        parse_events_page('{"entries": []}')
    with pytest.raises(ValueError):
        parse_events_page("[]")
