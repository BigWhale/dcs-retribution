"""The link to DCS Companion: a thread that reads its stream of snapshots.

The thread asks the companion's data port `GET /v1/hello`, then holds
`GET /v1/stream` open (Server-Sent Events) and hands every `state` event to
`on_snapshot` as a `LiveSnapshot`. `mission` events are logged; `events`
events read the event log (below); any other event is ignored.

With `on_events`, the link also reads the companion's event log: after each
hello and on each `events` event of the stream it asks `GET /v1/events` from
the newest seq it handed over (`events_after` at first) until it has the
log's `lastSeq`, a page of at most `EVENTS_PAGE` entries at a time, and hands
each page to `on_events` as an `EventBatch`. Without `events_after` it starts
at the first hello's `lastSeq`, with an empty batch, so the log's history is
never read. Its cursor outlives a connection, so a reconnect misses nothing.
A hello whose `lastSeq` is below the cursor (the companion's log is not the
one the cursor was read from, or was cut short) is warned about once per
connection; nothing new is handed over until the log passes the cursor.
An entry that cannot be read is skipped, with one warning per page, and the
cursor moves past it all the same.

Whatever goes wrong (the companion not there, a refused token, a dropped or
silent stream, a bad event, a failed read of the log) is logged once per kind
until a snapshot comes again and tried again `retry` seconds later: the
thread only ends on `stop()`.

The callbacks run on this thread; a Qt caller turns them into queued signals
and must not block on the GUI thread inside them (`stop()` waits for a
callback that is running).
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any, Callable, Iterable, Iterator, Optional, TypeVar

import httpx

from game.companionlink.eventlog import EventBatch, parse_events_page
from game.companionlink.linkstatus import LinkStatus as LinkStatus  # re-exported
from game.companionlink.snapshot import LiveSnapshot, parse_snapshot

log = logging.getLogger("game.companionlink")

#: The campaign protocol this link speaks (the companion's `gm.Protocol`).
PROTOCOL = 1
#: How long `stop()` waits for the thread.
STOP_WAIT = 2.0
#: Connect, write and pool timeout of every request; the stream's read
#: timeout is the link's own `read_timeout`.
CONNECT_TIMEOUT = 5.0
#: Entries asked per `GET /v1/events` (the companion's most).
EVENTS_PAGE = 5000

T = TypeVar("T")


class _Failure(Exception):
    """A failed attempt: its kind (logged once), what to log, the new status."""

    def __init__(self, kind: str, message: str, status: LinkStatus) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


class CompanionLink(threading.Thread):
    def __init__(
        self,
        address: str,
        token: str,
        on_snapshot: Callable[[LiveSnapshot], None],
        on_status: Callable[[LinkStatus], None],
        retry: float = 5.0,
        read_timeout: float = 90.0,
        on_events: Optional[Callable[[EventBatch], None]] = None,
        events_after: Optional[int] = None,
    ) -> None:
        super().__init__(name="CompanionLink", daemon=True)
        self._address = address.rstrip("/")
        self._token = token
        self._on_snapshot = on_snapshot
        self._on_status = on_status
        self._retry = retry
        self._read_timeout = read_timeout
        self._on_events = on_events
        # The newest seq handed to on_events; None until the first hello.
        self._after: Optional[int] = events_after
        # The last hello's lastSeq, when it had one.
        self._hello_last: Optional[int] = None
        self._halt = threading.Event()
        # Guards _halted and _sock, and holds a callback while it runs, so
        # none starts once stop() has set _halted.
        self._lock = threading.RLock()
        self._halted = False
        self._sock: Optional[socket.socket] = None
        self._status: Optional[LinkStatus] = None
        # The kinds of failure logged since the last snapshot.
        self._failures: set[str] = set()

    def stop(self) -> None:
        """Ends the loop and the open stream; returns after the join (≤ 2 s)."""
        with self._lock:
            self._halted = True
            self._halt.set()
            sock = self._sock
        if sock is not None:
            try:
                # Wakes a read blocked on the stream, which close() does not.
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.is_alive() and threading.current_thread() is not self:
            self.join(STOP_WAIT)

    def run(self) -> None:
        log.info("companion link: started for %s", self._address)
        while not self._halt.is_set():
            try:
                self._connect()
                raise _Failure(
                    "dropped", "the companion ended the stream", LinkStatus.WAITING
                )
            except _Failure as f:
                self._failed(f.kind, str(f), f.status)
            except httpx.ConnectError as e:
                self._failed(
                    "connect",
                    f"cannot reach the companion at {self._address}: {e}",
                    LinkStatus.WAITING,
                )
            except httpx.TimeoutException as e:
                self._failed(
                    "timeout",
                    f"the companion went silent ({type(e).__name__})",
                    LinkStatus.WAITING,
                )
            except httpx.TransportError as e:
                self._failed(
                    "dropped",
                    f"the companion's stream dropped: {type(e).__name__}: {e}",
                    LinkStatus.WAITING,
                )
            except Exception as e:
                self._failed(
                    type(e).__name__,
                    f"companion link failed: {type(e).__name__}: {e}",
                    LinkStatus.WAITING,
                )
            self._halt.wait(self._retry)
        log.info("companion link: stopped")

    def _connect(self) -> None:
        """One connection: hello, then the stream until it ends."""
        headers = {"Authorization": f"Bearer {self._token}"}
        timeout = httpx.Timeout(CONNECT_TIMEOUT, read=self._read_timeout)
        with httpx.Client(headers=headers, timeout=timeout) as client:
            hello = client.get(f"{self._address}/v1/hello")
            self._check(hello, "hello")
            self._hello(hello)
            self._fetch_events(client, after_hello=True)
            with client.stream(
                "GET",
                f"{self._address}/v1/stream",
                headers={"Accept": "text/event-stream"},
            ) as resp:
                self._check(resp, "stream")
                self._hold(resp)
                try:
                    if self._halt.is_set():
                        return
                    self._set_status(LinkStatus.LINKED)
                    self._read(resp.iter_bytes(), client)
                finally:
                    with self._lock:
                        self._sock = None

    def _check(self, resp: httpx.Response, what: str) -> None:
        if resp.status_code == 401:
            raise _Failure(
                "refused",
                "the companion refused the data token (401)",
                LinkStatus.REFUSED,
            )
        if resp.status_code != 200:
            raise _Failure(
                f"status {resp.status_code}",
                f"the companion answered {what} with {resp.status_code}",
                LinkStatus.WAITING,
            )

    def _hello(self, resp: httpx.Response) -> None:
        try:
            hello = resp.json()
        except ValueError as e:
            raise _Failure(
                "hello", f"the companion's hello is not JSON: {e}", LinkStatus.WAITING
            )
        if not isinstance(hello, dict):
            raise _Failure(
                "hello", "the companion's hello is not an object", LinkStatus.WAITING
            )
        log.info(
            "companion hello: protocol %s, boot %s, session %s",
            hello.get("protocol"),
            hello.get("boot"),
            hello.get("session"),
        )
        last = hello.get("lastSeq")
        self._hello_last = (
            last if isinstance(last, int) and not isinstance(last, bool) else None
        )
        if hello.get("protocol") != PROTOCOL:
            self._once(
                "protocol",
                f"the companion speaks protocol {hello.get('protocol')}, "
                f"this link {PROTOCOL}",
            )

    def _hold(self, resp: httpx.Response) -> None:
        """Keeps the stream's socket, for stop() to wake a blocked read."""
        stream: Any = resp.extensions.get("network_stream")
        sock = stream.get_extra_info("socket") if stream is not None else None
        with self._lock:
            self._sock = sock if isinstance(sock, socket.socket) else None
            halted = self._halted
        if halted and self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _fetch_events(self, client: httpx.Client, after_hello: bool = False) -> None:
        """Reads the event log from the cursor to its lastSeq; a failure
        raises, and the next connection reads again from the cursor. Only the
        read right after a hello compares the cursor with the hello's lastSeq,
        once per connection, or starts a cursor from it: a notice's read finds
        the hello's lastSeq stale, so without a cursor it waits for the next
        hello."""
        if self._on_events is None:
            return
        if self._after is None:
            if not after_hello:
                return
            if self._hello_last is None:
                self._once("no lastSeq", "the companion's hello has no lastSeq")
                return
            if self._deliver(self._on_events, EventBatch((), self._hello_last)):
                self._after = self._hello_last
                log.info("companion events: a new cursor at %d", self._after)
            return
        if (
            after_hello
            and self._hello_last is not None
            and self._hello_last < self._after
        ):
            log.warning(
                "Live losses: the companion's event log ends at %d, before this "
                "campaign's cursor %d; nothing is read until it catches up",
                self._hello_last,
                self._after,
            )
        while not self._halt.is_set():
            resp = client.get(
                f"{self._address}/v1/events",
                params={"after": self._after, "limit": EVENTS_PAGE},
            )
            self._check(resp, "events")
            try:
                page = parse_events_page(resp.text)
            except ValueError as e:
                raise _Failure(
                    "events", f"the companion's events are bad: {e}", LinkStatus.WAITING
                )
            if page.skipped:
                log.warning(
                    "companion events: %d entries after %d skipped",
                    page.skipped,
                    self._after,
                )
            if page.through <= self._after:
                return  # empty, or nothing with a seq to move past
            if page.entries:
                log.debug(
                    "companion events: %d entries through %d (log at %d)",
                    len(page.entries),
                    page.through,
                    page.last_seq,
                )
                batch = EventBatch(page.entries, page.through)
                if not self._deliver(self._on_events, batch):
                    return
            self._after = page.through
            if page.through >= page.last_seq:
                return

    def _read(self, chunks: Iterable[bytes], client: httpx.Client) -> None:
        """Dispatches each event of the stream at its blank line."""
        kind = ""
        data: list[bytes] = []
        for line in _lines(chunks):
            if self._halt.is_set():
                return
            if not line:
                if data:
                    self._dispatch(kind, b"\n".join(data), client)
                kind, data = "", []
                continue
            if line.startswith(b":"):
                continue
            field, _, value = line.partition(b":")
            if value.startswith(b" "):
                value = value[1:]
            if field == b"event":
                kind = value.decode("utf-8", errors="replace")
            elif field == b"data":
                data.append(value)

    def _dispatch(self, kind: str, raw: bytes, client: httpx.Client) -> None:
        if kind == "state":
            try:
                snap = parse_snapshot(raw.decode("utf-8"))
            except ValueError as e:
                self._once("bad event", f"companion state event skipped: {e}")
                return
            log.debug(
                "companion snapshot: minute %d, %d units, %d statics, %d bytes",
                snap.minute,
                snap.unit_count,
                len(snap.units) - snap.unit_count,
                snap.size,
            )
            self._failures.clear()
            self._set_status(LinkStatus.RECEIVING)
            self._deliver(self._on_snapshot, snap)
        elif kind == "events":
            log.debug("companion events: %s", raw.decode("utf-8", errors="replace"))
            self._fetch_events(client)
        elif kind == "mission":
            log.info(
                "companion mission: %s", raw.decode("utf-8", errors="replace")[:200]
            )
        else:
            log.debug("companion event %r ignored", kind)

    def _failed(self, kind: str, message: str, status: LinkStatus) -> None:
        if self._halt.is_set():
            return
        self._once(kind, message)
        self._set_status(status)

    def _once(self, kind: str, message: str) -> None:
        """Logs a warning once per kind until the next snapshot, then debug."""
        if kind in self._failures:
            log.debug("%s (again)", message)
            return
        self._failures.add(kind)
        log.warning("%s", message)

    def _set_status(self, status: LinkStatus) -> None:
        if status == self._status:
            return
        if self._deliver(self._on_status, status):
            self._status = status
            log.info("companion link: %s", status.value)

    def _deliver(self, callback: Callable[[T], None], value: T) -> bool:
        """Runs a callback unless stopped; False when stopped."""
        with self._lock:
            if self._halted:
                return False
            try:
                callback(value)
            except Exception:
                log.exception("companion link: a callback failed")
            return True


def _lines(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """The stream's lines, without their line end.

    Split on LF only (a CR before it dropped), never on the other line breaks
    `str.splitlines` knows: a unit's name may hold U+2028 or U+0085 as they
    are, and a snapshot is one line however long.
    """
    buf = bytearray()
    for chunk in chunks:
        start = 0
        while True:
            end = chunk.find(b"\n", start)
            if end < 0:
                buf += chunk[start:]
                break
            buf += chunk[start:end]
            if buf.endswith(b"\r"):
                del buf[-1]
            yield bytes(buf)
            buf.clear()
            start = end + 1
