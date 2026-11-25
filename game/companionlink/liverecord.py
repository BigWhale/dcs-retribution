"""A dynamic campaign's live record: which live mission is its own, and the
event log entries waiting for it.

Begin Campaign's mission carries a hidden marker static named
`MARKER_PREFIX + uuid`, a new uuid per written mission. The first snapshot of
a mission (a `boot`/`session` pair) decides it: the campaign's own when it
names the static of the uuid written last, someone else's otherwise. Only
the campaign's own mission is followed; entries of the event log wait until
their mission is decided, and only the accepted mission's are applied.

The record keeps the newest snapshot of the campaign's own mission under the
name map's uuid as its tick (compressed), so the tick and the name map always
belong to the same mission; a restored mission is written from it.

The campaign's mission loaded again from its start (a new `boot`/`session`
pair with a marker already followed: DCS restarted with the file) is not
followed; the dead would live again in DCS.

The record is saved with the game: the marker, the name map, the verdicts,
the log cursor, the names already counted dead or seen landing, the tick (a
save from before the tick loads with none), where the save stood
(`saved_at`), the highest spawn asked, the reason a mission was not picked up
and the newest mission file written. The current mission is not saved: a
reloaded game follows none until a snapshot decides one, as after a lost
link. Entries held for a mission not yet decided are not saved; the link
reads them again from the cursor.

A loaded game awaits a pick-up check (`pick_up`) before it follows anything:
its saved verdicts may name a mission that has moved on without it. Until the
check decides, no entry is handed out and no snapshot kept. The check picks
the running mission up only when it is the save's own, at most `PICKUP_S`
past the save in mission time, and holds no spawn the save does not know;
otherwise that mission is not followed (`MOVED_ON`), and its entries are
dropped.
"""

from __future__ import annotations

import logging
import uuid as uuidlib
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, TYPE_CHECKING

from game.companionlink.eventlog import EventBatch, LogEntry
from game.companionlink.linkstatus import LinkStatus
from game.companionlink.liveunitmap import LiveUnitMap
from game.companionlink.snapshot import LiveSnapshot, parse_snapshot

if TYPE_CHECKING:
    # Not at run time: the game loads without httpx.
    from game.companionlink.commands import SpawnsView

log = logging.getLogger("game.companionlink")

#: The marker static's name, before the uuid of the mission written.
MARKER_PREFIX = "dcs-companion-mission|"
#: The most missions whose verdicts are kept: the newest, the current one
#: always. An older mission's entries are far behind the cursor.
VERDICTS_KEPT = 64
#: How far past the save, in mission seconds, a loaded game still picks up
#: the running mission.
PICKUP_S = 300.0


class MissionVerdict(Enum):
    OWN = "own"
    #: The campaign's own mission, loaded again from its start (a DCS restart
    #: with its file): units killed earlier are alive again in DCS. Not
    #: followed; Continue loads it as it stood.
    OWN_AGAIN = "own again"
    #: The campaign's own mission, which a loaded save did not pick up: it has
    #: gone on too long or launched flights the save does not know. Not
    #: followed.
    MOVED_ON = "moved on"
    FOREIGN = "foreign"

    @property
    def followed(self) -> bool:
        """Whether a mission of this verdict is followed: its snapshots kept,
        its entries applied, flights launched into it."""
        return self in _FOLLOWED


_FOLLOWED = (MissionVerdict.OWN,)


@dataclass(frozen=True)
class SavedAt:
    """Where a save stood, for the pick-up check of a loaded game."""

    #: The mission (boot, session) followed at the save; None when none was.
    mission: Optional[tuple[str, int]]
    #: Its model time at the save; None when none was followed or the clock
    #: had no sample.
    model_time: Optional[float]
    #: The highest spawn id number asked for (`s-N` is N); None before the
    #: first.
    spawn: Optional[int]


@dataclass(frozen=True)
class PickUp:
    """What the pick-up check made of the running mission."""

    #: Whether it decided: False when the companion's spawns describe another
    #: mission than the snapshot (the check waits for the next snapshot).
    decided: bool
    #: Whether the running mission is picked up.
    picked: bool
    #: The running mission's model time past the save's.
    seconds: float
    #: Why it was not picked up (the status line's words); None when it was,
    #: or when nothing was decided.
    reason: Optional[str]


@dataclass(frozen=True)
class LiveTick:
    """The newest snapshot of the campaign's own mission, kept compressed."""

    boot: str
    session: int
    minute: int
    model_time: float
    abs_time: datetime
    #: The snapshot's envelope, zlib-compressed.
    packed: bytes = field(repr=False)

    def snapshot(self) -> LiveSnapshot:
        """The snapshot, parsed again from its envelope."""
        return parse_snapshot(zlib.decompress(self.packed).decode("utf-8"))


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
        #: The missions decided, oldest first, at most `VERDICTS_KEPT`:
        #: (boot, session) -> (verdict, its uuid).
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
        #: The newest snapshot of the mission `name_map` belongs to.
        self.tick: Optional[LiveTick] = None
        #: Where the newest save stood.
        self.saved_at = SavedAt(None, None, None)
        #: The highest spawn id number asked for; None before the first.
        self.last_spawn: Optional[int] = None
        #: Why the last mission refused by a pick-up was not picked up.
        self.moved_on_reason: Optional[str] = None
        #: The file name of the newest mission written.
        self.mission_file: Optional[str] = None
        #: A loaded game: nothing is followed until the pick-up check decides.
        self.awaiting_pickup = False
        # The pick-up of this load refused the running mission.
        self._pickup_refused = False
        self._pending: list[LogEntry] = []
        self._told_no_marker = False

    def __getstate__(self) -> dict[str, Any]:
        state = self.__dict__.copy()
        del state["current"]
        del state["awaiting_pickup"]
        del state["_pickup_refused"]
        del state["_pending"]
        del state["_told_no_marker"]
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        state.setdefault("tick", None)  # a save from before the tick
        # A save from before the pick-up: it never picks up.
        state.setdefault("saved_at", SavedAt(None, None, None))
        state.setdefault("last_spawn", None)
        state.setdefault("moved_on_reason", None)
        state.setdefault("mission_file", None)
        self.__dict__.update(state)
        self.current = None
        self.awaiting_pickup = True
        self._pickup_refused = False
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
            # The name map's uuid is the marker of the mission followed last.
            if uuid == self.map_marker:
                verdict = MissionVerdict.OWN_AGAIN
                log.warning(
                    "Live losses: the campaign's mission was loaded again from "
                    "its start (boot %s, session %d); not followed",
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
                # The old map's tick goes with it: the new mission's first
                # snapshot that can be kept is the tick again.
                self.tick = None
        else:
            verdict = MissionVerdict.FOREIGN
            log.info(
                "Live losses: boot %s, session %d is not this campaign's mission",
                *pair,
            )
        self.verdicts[pair] = (verdict, uuid)
        self._forget_old_verdicts()
        return verdict

    def keep(self, snapshot: LiveSnapshot) -> bool:
        """Keeps the snapshot as the tick when its mission is followed under
        the name map's uuid, no pick-up is awaited, it has its absolute time
        and its envelope, and it is later in mission time than the tick held;
        whether it did."""
        if self.awaiting_pickup:
            return False
        known = self.verdicts.get((snapshot.boot, snapshot.session))
        if known is None:
            return False
        verdict, uuid = known
        if not verdict.followed or uuid != self.map_marker:
            return False
        if snapshot.abs_time is None or not snapshot.packed:
            return False
        if self.tick is not None and snapshot.abs_time <= self.tick.abs_time:
            return False
        self.tick = LiveTick(
            boot=snapshot.boot,
            session=snapshot.session,
            minute=snapshot.minute,
            model_time=snapshot.model_time,
            abs_time=snapshot.abs_time,
            packed=snapshot.packed,
        )
        return True

    def retire(self) -> None:
        """The campaign's missions decided so far are no longer its own: a
        restored mission replaces them. Each becomes another mission (its
        uuid kept), so none is followed or launched into again and their
        entries are dropped; the restored mission's first snapshot is decided
        the campaign's own by its new marker."""
        for pair, (verdict, uuid) in self.verdicts.items():
            if verdict.followed:
                self.verdicts[pair] = (MissionVerdict.FOREIGN, uuid)

    def spawn_asked(self, number: int) -> None:
        """The companion gave the spawn id `s-<number>`; the highest is kept."""
        if self.last_spawn is None or number > self.last_spawn:
            self.last_spawn = number

    def mark_saved(self, model_time: Optional[float]) -> None:
        """The game is about to be saved, the mission clock at `model_time`
        (None without a sample): records where the save stands. Not while a
        pick-up is awaited or after one refused the running mission in this
        load: the save keeps where it stood, so loading it again gives the
        same answer."""
        if self.awaiting_pickup or self._pickup_refused:
            return
        following = self.following_own_mission
        self.saved_at = SavedAt(
            self.current if following else None,
            model_time if following else None,
            self.last_spawn,
        )

    def pick_up(self, snapshot: LiveSnapshot, view: SpawnsView) -> PickUp:
        """The pick-up check of a loaded game, on the running mission's
        snapshot and the companion's view of its spawns.

        Picks the mission up when it is the save's own (followed at the save,
        or written last and never followed, its clock then counting from 0),
        at most `PICKUP_S` past the save's model time, and holds no spawn
        over the save's highest, failed ones aside (when both fail, the
        flights are the reason given). A view of another mission
        than the snapshot's decides nothing. Once decided, the gate is off;
        a refused mission of the campaign's is `MOVED_ON`, another mission is
        left to `decide`.
        """
        pair = (snapshot.boot, snapshot.session)
        if (view.boot, view.session) != pair:
            return PickUp(False, False, 0.0, None)
        saved = self.saved_at
        known = self.verdicts.get(pair)
        base: Optional[float] = None
        if (
            saved.mission == pair
            and saved.model_time is not None
            and known is not None
            # MOVED_ON: refused in an earlier load and saved since.
            and known[0] in (MissionVerdict.OWN, MissionVerdict.MOVED_ON)
        ):
            base = saved.model_time
            uuid = known[1]
        elif (
            self.marker is not None
            and self.marker != self.map_marker
            and MARKER_PREFIX + self.marker in snapshot.names
        ):
            base = 0.0
            uuid = self.marker
        elif known is not None and known[0].followed:
            # The campaign's own, but the save cannot tell where it stood.
            uuid = known[1]
        else:
            uuid = None
        seconds = 0.0 if base is None else view.model_time - base
        unknown = [
            number
            for number, status in view.spawns
            if status != "failed" and (saved.spawn is None or number > saved.spawn)
        ]
        reason: Optional[str] = None
        if base is None:
            reason = "another mission is running"
        elif unknown:
            # Named before the time: the flights say more about the save.
            flights = "flight" if len(unknown) == 1 else "flights"
            reason = (
                f"the server's mission has {len(unknown)} {flights} "
                "this save never launched"
            )
        elif seconds > PICKUP_S:
            reason = f"the server's mission is {_minutes(seconds)} past this save"
        self.awaiting_pickup = False
        if reason is None:
            log.info(
                "Pick-up: picked up boot %s session %d, %.0f s after the save",
                *pair,
                seconds,
            )
            return PickUp(True, True, seconds, None)
        log.info("Pick-up: not picked up: %s", reason)
        self._pickup_refused = True
        if uuid is not None:
            self.verdicts[pair] = (MissionVerdict.MOVED_ON, uuid)
            self.moved_on_reason = reason
            self._forget_old_verdicts()
        return PickUp(True, False, seconds, reason)

    def end_pickup(self) -> None:
        """Continue loads the campaign's mission: no pick-up is awaited any
        more, and the next save records where it stands again. Decides
        nothing."""
        self.awaiting_pickup = False
        self._pickup_refused = False

    def _forget_old_verdicts(self) -> None:
        """Drops the oldest verdicts past `VERDICTS_KEPT`, never the current
        mission's."""
        excess = len(self.verdicts) - VERDICTS_KEPT
        if excess <= 0:
            return
        old = [p for p in self.verdicts if p != self.current][:excess]
        for pair in old:
            del self.verdicts[pair]

    def lose(self) -> None:
        """The link is lost or stale: no mission is current. Verdicts stay."""
        self.current = None

    def verdict_of(self, mission: tuple[str, int]) -> Optional[MissionVerdict]:
        """The verdict on the mission (boot, session); None when it is not
        decided (or long forgotten)."""
        known = self.verdicts.get(mission)
        return known[0] if known is not None else None

    @property
    def current_verdict(self) -> Optional[MissionVerdict]:
        """The current mission's verdict; None when there is none."""
        if self.current is None:
            return None
        return self.verdict_of(self.current)

    @property
    def following_own_mission(self) -> bool:
        """The current mission is the campaign's own, followed; never while a
        pick-up is awaited."""
        verdict = self.current_verdict
        return not self.awaiting_pickup and verdict is not None and verdict.followed

    @property
    def has_held(self) -> bool:
        """Entries are held: waiting for their mission's verdict or for
        `ready`."""
        return bool(self._pending)

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
        `limit` of them; none while a pick-up is awaited.

        Stops at the first entry of a mission not yet decided. Entries of a
        mission decided against, or of one of the campaign's earlier missions,
        are dropped and the cursor moves past them, but only before any entry
        is handed out: after one, the call stops at the next entry to drop,
        which waits for the next call, once `applied` has moved the cursor. So
        the cursor never passes an entry handed out and not applied yet.
        """
        out: list[LogEntry] = []
        if self.awaiting_pickup:
            return out
        taken = 0
        for e in self._pending:
            if limit is not None and len(out) >= limit:
                break
            known = self.verdicts.get(e.mission)
            if known is None:
                break
            verdict, uuid = known
            if not verdict.followed or uuid != self.map_marker:
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


def _minutes(seconds: float) -> str:
    """`N minute(s)`, N the whole minutes of `seconds`, rounded down."""
    n = max(0, int(seconds // 60))
    return f"{n} {'minute' if n == 1 else 'minutes'}"


def picked_up_note(seconds: float) -> str:
    """The status's line under the mission time after a pick-up `seconds`
    past the save."""
    return f"Picked up the running mission, {_minutes(seconds)} after the save"


#: The tail of a status line whose mission is not followed.
_CONTINUE = "; Continue to load the campaign's mission"


def companion_status(
    status: LinkStatus,
    snapshot: Optional[LiveSnapshot],
    stale: bool,
    verdict: Optional[MissionVerdict],
    *,
    note: Optional[str] = None,
    reason: Optional[str] = None,
    loading: Optional[str] = None,
    load_failed: Optional[str] = None,
    awaiting: bool = False,
) -> str:
    """The companion's status in the top panel; `verdict` is the current
    mission's, None when undecided. `note` is a line under the mission time
    (`picked_up_note`); `reason` why a `MOVED_ON` mission was not picked up;
    `loading` the mission file a load waits for, `load_failed` the one that
    did not load; `awaiting` while the pick-up waits for the command port."""
    if status is LinkStatus.WAITING:
        return "Companion: not running"
    if status is LinkStatus.REFUSED:
        return "Companion: refused (token)"
    if loading is not None:
        return f"Companion: loading {loading} on the server…"
    if load_failed is not None:
        return (
            f"Companion: {load_failed} was not loaded on the server; "
            "Continue to try again"
        )
    if status is LinkStatus.LINKED or snapshot is None or stale:
        return "Companion: waiting for a mission on the server"
    if awaiting:
        return "Companion: waiting for the command port to check the running mission"
    if verdict is None:
        return "Companion: waiting for a mission on the server"
    if verdict is MissionVerdict.OWN_AGAIN:
        return (
            "Companion: the campaign's mission was loaded again from its start; "
            "Continue to load it as it stood"
        )
    if verdict is MissionVerdict.MOVED_ON and reason is not None:
        return f"Companion: {reason}{_CONTINUE}"
    if not verdict.followed:
        return f"Companion: another mission is running{_CONTINUE}"
    unit = "minute" if snapshot.minute == 1 else "minutes"
    text = f"DCS Companion time: {snapshot.minute} {unit}"
    if note is not None:
        text += "\n" + note
    return text
