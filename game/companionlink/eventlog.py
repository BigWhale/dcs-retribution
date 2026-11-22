"""The companion's event log, as `GET /v1/events` serves it.

Each entry is one thing that happened in a mission (a death, a kill, a
landing, a spawn), numbered by `seq` from 1 across missions and restarts of
the companion. Only what the campaign needs is kept: the entry's number, kind
and mission, the units it names, and where and to whom it happened. Fields
this side has no use for (`modelTime`, `time`, `hookN`, `weapon`, `spawn`,
`reason`, `pos.lat`, `pos.lon`) are ignored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class LogRef:
    """A unit or object an entry names; a field the hook could not read is
    empty (a scenery object's name is its DCS number, as a string)."""

    name: str
    type: str = ""
    side: Optional[int] = None  # 0 neutral, 1 red, 2 blue
    category: str = ""  # plane, helicopter, ground, ship, static, scenery
    group: str = ""
    player: str = ""


@dataclass(frozen=True)
class LogPos:
    x: float  # map metres, north
    y: float  # map metres, east
    alt: float  # metres


@dataclass(frozen=True)
class LogEntry:
    seq: int
    kind: str
    boot: str
    session: int
    unit: Optional[LogRef] = None
    other: Optional[LogRef] = None
    place: str = ""  # an airbase's name
    side: Optional[int] = None  # base_captured: the new side
    group: str = ""
    pos: Optional[LogPos] = None

    @property
    def mission(self) -> tuple[str, int]:
        """The mission the entry belongs to: (boot, session)."""
        return self.boot, self.session


@dataclass(frozen=True)
class EventsPage:
    entries: tuple[LogEntry, ...]
    last_seq: int  # the log's last seq when the page was read
    skipped: int  # entries that could not be read


@dataclass(frozen=True)
class EventBatch:
    """Entries the link hands over, oldest first.

    `through` is the last seq the batch covers; an empty batch whose
    `through` is the hello's `lastSeq` starts a new cursor there.
    """

    entries: tuple[LogEntry, ...]
    through: int


def parse_events_page(data: str) -> EventsPage:
    """A page of the log; ValueError on bad JSON or no whole `lastSeq`.

    An entry that is not an object or lacks `seq`, `kind`, `boot` or
    `session` is skipped and counted, so one odd line never costs the page.
    """
    body = json.loads(data)
    if not isinstance(body, dict):
        raise ValueError("events: not a JSON object")
    last_seq = _whole(body.get("lastSeq"))
    if last_seq is None:
        raise ValueError("events: missing lastSeq")
    raw = body.get("entries")
    if not isinstance(raw, list):
        raw = []
    entries: list[LogEntry] = []
    skipped = 0
    for item in raw:
        entry = _entry(item)
        if entry is None:
            skipped += 1
        else:
            entries.append(entry)
    return EventsPage(tuple(entries), last_seq, skipped)


def _entry(item: Any) -> Optional[LogEntry]:
    if not isinstance(item, dict):
        return None
    seq = _whole(item.get("seq"))
    session = _whole(item.get("session"))
    kind = item.get("kind")
    boot = item.get("boot")
    if seq is None or session is None:
        return None
    if not isinstance(kind, str) or not isinstance(boot, str):
        return None
    return LogEntry(
        seq=seq,
        kind=kind,
        boot=boot,
        session=session,
        unit=_ref(item.get("unit")),
        other=_ref(item.get("other")),
        place=_text(item.get("place")),
        side=_whole(item.get("side")),
        group=_text(item.get("group")),
        pos=_pos(item.get("pos")),
    )


def _ref(item: Any) -> Optional[LogRef]:
    if not isinstance(item, dict):
        return None
    return LogRef(
        name=_text(item.get("name")),
        type=_text(item.get("type")),
        side=_whole(item.get("side")),
        category=_text(item.get("category")),
        group=_text(item.get("group")),
        player=_text(item.get("player")),
    )


def _pos(item: Any) -> Optional[LogPos]:
    if not isinstance(item, dict):
        return None
    x, y, alt = _number(item.get("x")), _number(item.get("y")), _number(item.get("alt"))
    if x is None or y is None or alt is None:
        return None
    return LogPos(x, y, alt)


def _whole(v: Any) -> Optional[int]:
    """An int that is not a bool, else None."""
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    return None


def _number(v: Any) -> Optional[float]:
    """An int or float that is not a bool, as a float, else None."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def _text(v: Any) -> str:
    return v if isinstance(v, str) else ""
