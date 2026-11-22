"""Live losses: the companion's event log applied to a dynamic campaign.

A turn learns what died from the mission's debrief: name lists that
`Debriefing` looks up in the turn's `UnitMap`, applied by
`MissionResultsProcessor`. A dynamic campaign plays one mission for as long as
it runs, so it applies the event log's entries as they come, in batches: each
batch becomes the same name lists, debriefed against the saved name map
(`game.live.name_map`) and applied by the turn's loss steps
(`MissionResultsProcessor.commit_live`), once per batch.

A unit is lost once: the names counted dead are kept in `game.live.dead`, in
this batch and every later one, across saves. Names the map does not know
(the marker, CTLD crates, a script's units) change nothing but the wrecks: as
a turn's script records every dead unit, each death with a position and a
type that is not a crash model leaves one, once per name. A launched flight
whose every unit is dead or landed is finished, and a flight not launched
whose base was captured is stranded; both are returned for the caller to
remove (the GUI through its models, `finish_flight` headless), then `settle`
frees the squadrons' aircraft and pilots and redraws the threat zones. A
finished flight's landed aircraft keep no pilot in the map: the pilots went
home with the flight, so one destroyed on the ramp later costs the aircraft
only, as a parked aircraft does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, TYPE_CHECKING, Union

from game.ato.flight import Flight
from game.companionlink.eventlog import LogEntry, LogPos, LogRef
from game.companionlink.liverecord import LiveRecord
from game.companionlink.liveunitmap import LiveUnitMap
from game.debriefing import Debriefing
from game.sim.gameupdateevents import GameUpdateEvents
from game.sim.missionresultsprocessor import MissionResultsProcessor
from game.squadrons import Squadron
from game.theater import Airfield, Carrier, ControlPoint, Lha, OffMapSpawn
from game.unitmap import FlyingUnit

if TYPE_CHECKING:
    from game import Game

#: Kinds whose `unit` is lost; a `kill`'s victim is its `other`.
DEATHS = frozenset({"dead", "unit_lost", "crash", "pilot_dead", "ejection"})

#: A dead object whose type holds one of these is debris, not a wreck (as
#: Retribution's mission script filters its `destroyed_objects_positions`).
CRASH_MODELS = ("_CRASH", "GENERIC_CRASH_MODEL")

#: Control points Retribution cannot capture.
UNCAPTURABLE = (Carrier, Lha, OffMapSpawn)


@dataclass
class LiveBatch:
    """What one batch of entries did to the game."""

    entries: int
    #: Names counted dead in this batch (a scenery target by its zone's name).
    dead: list[str]
    captured: list[ControlPoint]
    #: Launched flights whose every unit is dead or landed; not removed yet.
    finished: list[Flight]
    #: Flights not launched whose departure was captured; not removed yet.
    stranded: list[Flight]
    #: Squadrons that lost an aircraft or have a finished or stranded flight.
    squadrons: set[Squadron]
    #: A unit of an air-defence ground object died, or a base was captured.
    threat_changed: bool
    events: GameUpdateEvents


def apply_entries(game: Game, entries: Sequence[LogEntry]) -> LiveBatch:
    """Applies the accepted mission's entries, oldest first, as a turn's
    results are applied; one `Debriefing` for the batch."""
    record = game.live
    name_map = record.name_map
    dead: list[str] = []
    wrecks: list[dict[str, Union[float, str]]] = []
    captures: list[str] = []
    touched: list[str] = []  # aircraft dead or landed in this batch
    unknown_dead: set[str] = set()  # names the map does not know, dead here
    for e in sorted(entries, key=lambda e: e.seq):
        if e.kind == "land":
            if e.unit is not None and e.unit.name in name_map.aircraft:
                record.landed.add(e.unit.name)
                touched.append(e.unit.name)
        elif e.kind == "takeoff":
            if e.unit is not None:
                record.landed.discard(e.unit.name)
        elif e.kind == "base_captured":
            capture = _capture(game, e)
            if capture is not None:
                captures.append(capture)
        else:
            lost = _lost(e, record)
            if lost is None:
                continue
            name = _known_name(name_map, lost, e.pos)
            if name is None:
                # Not the campaign's, but its wreck is recorded as a turn's
                # script records every dead unit; once per name in the batch.
                if lost.name in unknown_dead:
                    continue
                unknown_dead.add(lost.name)
            elif name in record.dead:
                continue
            else:
                record.dead.add(name)
                dead.append(name)
                if name in name_map.aircraft:
                    touched.append(name)
            if e.pos is not None and lost.type and not _crash_model(lost.type):
                wrecks.append(
                    {
                        "x": e.pos.x,
                        "y": e.pos.alt,
                        "z": e.pos.y,
                        "type": lost.type,
                        "orientation": 0.0,
                    }
                )

    events = GameUpdateEvents()
    debriefing = Debriefing(
        {
            "mission_ended": False,
            "dead_events": dead,
            "destroyed_objects_positions": wrecks,
            "base_capture_events": captures,
        },
        game,
        name_map,
    )
    captured = [c.control_point for c in debriefing.base_captures]
    # Before the losses: a squadron retreating from a captured base takes its
    # flights' departure with it, and a dead unit no longer counts as air
    # defence.
    stranded = [
        f
        for coalition in (game.blue, game.red)
        for package in coalition.ato.packages
        for f in package.flights
        if not f.launched and any(f.departure is cp for cp in captured)
    ]
    threat_changed = bool(captured) or any(
        loss.theater_unit.ground_object.has_aa
        for loss in debriefing.ground_object_losses
    )
    squadrons = {loss.flight.squadron for loss in debriefing.air_losses.losses}

    MissionResultsProcessor(game).commit_live(debriefing, events)

    finished = _finished(record, touched)
    for flight in finished:
        for name in name_map.flight_units(flight):
            if name in record.landed:
                name_map.aircraft[name] = FlyingUnit(flight, None)
    squadrons.update(f.squadron for f in finished + stranded)
    return LiveBatch(
        entries=len(entries),
        dead=dead,
        captured=captured,
        finished=finished,
        stranded=stranded,
        squadrons=squadrons,
        threat_changed=threat_changed,
        events=events,
    )


def finish_flight(flight: Flight) -> None:
    """Removes a finished or stranded flight from its package, and the package
    from its ATO when it is left empty."""
    package = flight.package
    package.remove_flight(flight)
    ato = flight.squadron.coalition.ato
    if not package.flights and package in ato.packages:
        ato.remove_package(package)


def settle(game: Game, batch: LiveBatch) -> None:
    """After a batch's flights are removed: the squadrons' free aircraft and
    pilots are what their coalition's ATO does not hold, and the threat zones
    are drawn again when they changed."""
    for squadron in batch.squadrons:
        ato = squadron.coalition.ato
        squadron.return_all_but_held_by(
            f for package in ato.packages for f in package.flights
        )
    if batch.threat_changed:
        game.compute_threat_zones(batch.events)


def _lost(e: LogEntry, record: LiveRecord) -> Optional[LogRef]:
    """The unit an entry says is lost, if any.

    A unit that vanished is lost unless it was last seen landing (it parked
    and DCS removed it) or a player flew it; a despawned group never is.
    """
    if e.kind in DEATHS:
        return e.unit
    if e.kind == "kill":
        return e.other
    if e.kind == "vanished":
        unit = e.unit
        if unit is None or unit.player or unit.name in record.landed:
            return None
        return unit
    return None


def _known_name(
    name_map: LiveUnitMap, ref: LogRef, pos: Optional[LogPos]
) -> Optional[str]:
    """The name the map knows a lost unit by: a scenery object by the zone
    that holds its position, anything else by its own name. Airfields are
    left out: runway damage is a turn's."""
    if ref.category == "scenery":
        return name_map.scenery_at(pos.x, pos.y) if pos is not None else None
    name = ref.name
    if (
        name in name_map.aircraft
        or name in name_map.front_line_units
        or name in name_map.motorpool_units
        or name in name_map.theater_objects
        or name in name_map.convoys
        or name in name_map.cargo_ships
        or name in name_map.airlifts
    ):
        return name
    return None


def _crash_model(unit_type: str) -> bool:
    return any(m in unit_type for m in CRASH_MODELS)


def _capture(game: Game, e: LogEntry) -> Optional[str]:
    """A capture as the mission's trigger writes it, `"<cp.id>||<side>||<cp
    full name>"`; None for a place Retribution does not know or cannot
    capture, or a side that is not red or blue. A base the side already
    holds is left to `Debriefing`, which keeps the last capture of each."""
    if e.side not in (1, 2):
        return None
    cp = _control_point(game, e.place)
    if cp is None or isinstance(cp, UNCAPTURABLE):
        return None
    return f"{cp.id}||{e.side}||{cp.full_name}"


def _control_point(game: Game, place: str) -> Optional[ControlPoint]:
    """The control point DCS calls `place`: by name, or an airfield by its
    DCS airport's name."""
    if not place:
        return None
    for cp in game.theater.controlpoints:
        if cp.name == place or (isinstance(cp, Airfield) and cp.airport.name == place):
            return cp
    return None


def _finished(record: LiveRecord, touched: list[str]) -> list[Flight]:
    """Launched flights still in their package with a unit dead or landed in
    this batch, whose every unit is now dead or landed."""
    finished: list[Flight] = []
    for name in touched:
        flight = record.name_map.aircraft[name].flight
        if (
            not flight.launched
            or any(f is flight for f in finished)
            or not any(f is flight for f in flight.package.flights)
        ):
            continue
        units = record.name_map.flight_units(flight)
        if units and all(u in record.dead or u in record.landed for u in units):
            finished.append(flight)
    return finished
