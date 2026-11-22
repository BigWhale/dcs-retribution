"""A dynamic campaign's live record: which live mission is its own, and the
event log entries waiting for it.

Begin Campaign's mission carries a hidden marker static named
`MARKER_PREFIX + uuid`, a new uuid per written mission. The first snapshot of
a mission (a `boot`/`session` pair) decides it: the campaign's own when it
names the static of the uuid written last, someone else's otherwise. Only
the campaign's own mission is followed; entries of the event log wait until
their mission is decided, and only the accepted mission's are applied.

The record is saved with the game: the marker, the name map, the verdicts,
the log cursor and the names already counted dead or seen landing. The
current mission is not saved: a reloaded game follows none until a snapshot
decides one, as after a lost link. Entries held for a mission not yet decided
are not saved; the link reads them again from the cursor.
"""

from __future__ import annotations

import logging
import uuid as uuidlib
from enum import Enum
from typing import Any, Optional

from game.companionlink.eventlog import EventBatch, LogEntry
from game.companionlink.link import LinkStatus
from game.companionlink.liveunitmap import LiveUnitMap
from game.companionlink.snapshot import LiveSnapshot

log = logging.getLogger("game.companionlink")

#: The marker static's name, before the uuid of the mission written.
MARKER_PREFIX = "dcs-companion-mission|"


class MissionVerdict(Enum):
    OWN = "own"
    #: The campaign's own mission, loaded again (a DCS restart or a reload):
    #: units killed earlier are alive again in DCS, but stay dead in the game.
    OWN_AGAIN = "own again"
    FOREIGN = "foreign"


_FOLLOWED = (MissionVerdict.OWN, MissionVerdict.OWN_AGAIN)


class LiveRecord:
    def __init__(self) -> None:
        #: The uuid of the mission written last.
        self.marker: Optional[str] = None
        #: The name map of the mission written last.
        self.written_map: Optional[LiveUnitMap] = None
        #: The name map losses are debriefed against.
        self.name_map = LiveUnitMap()
        #: The uuid `name_map` belongs to.
        self.map_marker: Optional[str] = None
        #: Every mission decided: (boot, session) -> (verdict, its uuid).
        self.verdicts: dict[tuple[str, int], tuple[MissionVerdict, Optional[str]]] = {}
        #: The mission the newest snapshot came from; None while the link is
        #: lost or stale.
        self.current: Optional[tuple[str, int]] = None
        #: The newest seq of the event log dealt with.
        self.cursor: Optional[int] = None
        #: Unit names already counted dead.
        self.dead: set[str] = set()
        #: Aircraft names last seen landing.
        self.landed: set[str] = set()
        self._pending: list[LogEntry] = []
        self._told_no_marker = False

    def __getstate__(self) -> dict[str, Any]:
        state = self.__dict__.copy()
        del state["current"]
        del state["_pending"]
        del state["_told_no_marker"]
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self.current = None
        self._pending = []
        self._told_no_marker = False

    def new_marker(self) -> str:
        """A fresh uuid for the mission about to be written."""
        self.marker = str(uuidlib.uuid4())
        self.written_map = None
        return self.marker

    def written(self, name_map: LiveUnitMap) -> None:
        """The name map of the mission written under `marker`."""
        self.written_map = name_map

    def decide(self, snapshot: LiveSnapshot) -> MissionVerdict:
        """The verdict on the snapshot's mission, which becomes the current
        one; a mission is decided once, by its first snapshot."""
        pair = (snapshot.boot, snapshot.session)
        self.current = pair
        known = self.verdicts.get(pair)
        if known is not None:
            return known[0]
        verdict: MissionVerdict
        uuid: Optional[str] = None
        if self.marker is None:
            if not self._told_no_marker:
                log.warning(
                    "Live losses: this save has no mission marker; "
                    "start a new dynamic campaign"
                )
                self._told_no_marker = True
            verdict = MissionVerdict.FOREIGN
        elif MARKER_PREFIX + self.marker in snapshot.names:
            uuid = self.marker
            if any(u == uuid and v in _FOLLOWED for v, u in self.verdicts.values()):
                verdict = MissionVerdict.OWN_AGAIN
                log.warning(
                    "Live losses: the campaign's mission was loaded again "
                    "(boot %s, session %d); earlier losses stay",
                    *pair,
                )
            else:
                verdict = MissionVerdict.OWN
                log.info(
                    "Live losses: the campaign's mission is running "
                    "(boot %s, session %d)",
                    *pair,
                )
            if uuid != self.map_marker:
                self.name_map = (
                    self.written_map if self.written_map is not None else LiveUnitMap()
                )
                self.map_marker = uuid
                self.dead.clear()
                self.landed.clear()
        else:
            verdict = MissionVerdict.FOREIGN
            log.info(
                "Live losses: boot %s, session %d is not this campaign's mission",
                *pair,
            )
        self.verdicts[pair] = (verdict, uuid)
        return verdict

    def lose(self) -> None:
        """The link is lost or stale: no mission is current. Verdicts stay."""
        self.current = None

    @property
    def current_verdict(self) -> Optional[MissionVerdict]:
        """The current mission's verdict; None when there is none."""
        if self.current is None:
            return None
        known = self.verdicts.get(self.current)
        return known[0] if known is not None else None

    @property
    def following_own_mission(self) -> bool:
        """The current mission is the campaign's own."""
        return self.current_verdict in _FOLLOWED

    def receive(self, batch: EventBatch) -> None:
        """Holds a batch's entries until their mission is decided.

        An empty batch starts the cursor at its `through` when there is none
        yet; entries at or below the cursor were dealt with already.
        """
        if not batch.entries:
            if self.cursor is None:
                self.cursor = batch.through
            return
        held = {e.seq for e in self._pending}
        for e in batch.entries:
            if (self.cursor is None or e.seq > self.cursor) and e.seq not in held:
                self._pending.append(e)
                held.add(e.seq)
        self._pending.sort(key=lambda e: e.seq)

    def ready(self, limit: Optional[int] = None) -> list[LogEntry]:
        """The held entries of the accepted mission, oldest first, at most
        `limit` of them.

        Stops at the first entry of a mission not yet decided. Entries of a
        mission decided against, or of one of the campaign's earlier missions,
        are dropped and the cursor moves past them, but only before any entry
        is handed out: after one, the call stops at the next entry to drop,
        which waits for the next call, once `applied` has moved the cursor. So
        the cursor never passes an entry handed out and not applied yet.
        """
        out: list[LogEntry] = []
        taken = 0
        for e in self._pending:
            if limit is not None and len(out) >= limit:
                break
            known = self.verdicts.get(e.mission)
            if known is None:
                break
            verdict, uuid = known
            if verdict is MissionVerdict.FOREIGN or uuid != self.map_marker:
                if out:
                    break
                self.applied(e.seq)
            else:
                out.append(e)
            taken += 1
        del self._pending[:taken]
        return out

    def applied(self, seq: int) -> None:
        """Entries up to `seq` are dealt with."""
        self.cursor = seq if self.cursor is None else max(self.cursor, seq)


def companion_status(
    status: LinkStatus,
    snapshot: Optional[LiveSnapshot],
    stale: bool,
    verdict: Optional[MissionVerdict],
) -> str:
    """The companion's status in the top panel; `verdict` is the current
    mission's, None when undecided."""
    if status is LinkStatus.WAITING:
        return "Companion: not running"
    if status is LinkStatus.REFUSED:
        return "Companion: refused (token)"
    if status is LinkStatus.LINKED or snapshot is None or stale or verdict is None:
        return "Companion: waiting for a mission on the server"
    if verdict is MissionVerdict.FOREIGN:
        return "Companion: another mission is running; waiting for this campaign's"
    text = f"Companion: minute {snapshot.minute}, {snapshot.unit_count:,} units"
    if verdict is MissionVerdict.OWN_AGAIN:
        text += " (mission restarted: earlier losses stay)"
    return text
