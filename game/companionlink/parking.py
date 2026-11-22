"""Parking spots for a Cold or Warm launch from an airfield.

`plan_parking` picks the spots a flight is spawned on, from the newest snapshot
of the live mission: first the slots of parked spare aircraft of the flight's
type (the launcher despawns them before the spawn), then free slots that suit
the type. The snapshot's `x`/`y` are map metres in pydcs's frame (`x` north,
`y` east, DCS's `z`), so they are compared with `ParkingSlot.position` as they
are.

The snapshot can be a minute old, so the launcher keeps one `ParkingClaims` per
mission: a launch's slots and despawned groups are not used again until a
snapshot newer than the launch shows the field.

Only a dynamic campaign's launcher calls these.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from dcs.terrain import Airport
from dcs.terrain.terrain import ParkingSlot
from dcs.unittype import FlyingType

from game.companionlink.snapshot import LiveSnapshot, LiveUnit

#: A unit within this of a slot's position stands on that slot.
PARKING_RADIUS_M = 15.0
#: A spare stands still: slower than this.
SPARE_MAX_SPEED = 1.0  # m/s


@dataclass(frozen=True)
class ParkingPlan:
    airport_id: int  # the departure's DCS airport id
    slots: list[ParkingSlot]  # one per aircraft, spares' slots first
    despawn: list[str]  # spare group names to despawn, in slot order


@dataclass(frozen=True)
class _Claim:
    airport_id: int
    slots: frozenset[int]  # crossroad_idx
    despawn: frozenset[str]
    model_time: float


class ParkingClaims:
    """The spots and spares of the launches into one mission.

    A claim holds for every snapshot taken before it (`model_time` at or after
    the snapshot's): such a snapshot cannot show the launched flight on its
    slots nor the spares gone yet.
    """

    def __init__(self) -> None:
        self._claims: list[_Claim] = []

    def claim(self, plan: ParkingPlan, model_time: float) -> None:
        self._claims.append(
            _Claim(
                airport_id=plan.airport_id,
                slots=frozenset(s.crossroad_idx for s in plan.slots),
                despawn=frozenset(plan.despawn),
                model_time=model_time,
            )
        )

    def claimed_slots(self, airport_id: int, snapshot: LiveSnapshot) -> set[int]:
        """The crossroad_idx of slots claimed at or after the snapshot."""
        out: set[int] = set()
        for c in self._held(snapshot):
            if c.airport_id == airport_id:
                out |= c.slots
        return out

    def spent(self, snapshot: LiveSnapshot) -> set[str]:
        """The groups despawned at or after the snapshot."""
        out: set[str] = set()
        for c in self._held(snapshot):
            out |= c.despawn
        return out

    def _held(self, snapshot: LiveSnapshot) -> Iterable[_Claim]:
        return (c for c in self._claims if c.model_time >= snapshot.model_time)


def plan_parking(
    airport: Airport,
    aircraft: type[FlyingType],
    count: int,
    side: int,
    snapshot: LiveSnapshot,
    not_spares: Collection[str],
    claims: ParkingClaims,
) -> ParkingPlan | None:
    """`count` slots at `airport` for `aircraft` of `side`; None if too few.

    Spares, in snapshot order and whole groups only (none of more units than
    still needed), go first; `not_spares` names the groups never taken (the
    launcher's sent groups and every flight's live group). Then free slots, in
    pydcs's order for the type: no unit or static of the snapshot within
    `PARKING_RADIUS_M`, not claimed. Nothing of `airport` is changed.
    """
    near = _near(airport, snapshot.units)
    claimed = claims.claimed_slots(airport.id, snapshot)
    spent = claims.spent(snapshot)

    slots: list[ParkingSlot] = []
    despawn: list[str] = []
    used: set[int] = set()
    for name, units in _groups(near, snapshot.units).items():
        need = count - len(slots)
        if need <= 0:
            break
        if len(units) > need or name in not_spares or name in spent:
            continue
        spots = _spare_slots(airport, aircraft, side, units)
        if spots is None:
            continue
        idx = {s.crossroad_idx for s in spots}
        if idx & (used | claimed):
            continue
        slots.extend(spots)
        used |= idx
        despawn.append(name)

    need = count - len(slots)
    if need > 0:
        free = [
            s
            for s in _suitable(airport, aircraft)
            if s.crossroad_idx not in used
            and s.crossroad_idx not in claimed
            and not any(_on(u, s) for u in near)
        ]
        if len(free) < need:
            return None
        slots.extend(free[:need])
    return ParkingPlan(airport_id=airport.id, slots=slots, despawn=despawn)


def _suitable(airport: Airport, aircraft: type[FlyingType]) -> list[ParkingSlot]:
    """pydcs's free slots for the type, as if no slot were taken.

    The marks (`unit_id`) are Retribution's own mission generation's, not the
    live mission's; they are cleared for the call and put back in any case.
    """
    saved = [(s, s.unit_id) for s in airport.parking_slots]
    try:
        for s, _ in saved:
            s.unit_id = None
        return list(airport.free_parking_slots(aircraft))
    finally:
        for s, unit_id in saved:
            s.unit_id = unit_id


def _near(airport: Airport, units: Iterable[LiveUnit]) -> list[LiveUnit]:
    """The units close enough to the field to stand on one of its slots."""
    if not airport.parking_slots:
        return []
    cx, cy = airport.position.x, airport.position.y
    reach = PARKING_RADIUS_M + max(
        math.hypot(s.position.x - cx, s.position.y - cy) for s in airport.parking_slots
    )
    return [u for u in units if math.hypot(u.x - cx, u.y - cy) <= reach]


def _groups(
    near: list[LiveUnit], units: Iterable[LiveUnit]
) -> dict[str, list[LiveUnit]]:
    """Every group with a unit near the field, all its units, in snapshot order."""
    names = {u.group for u in near if u.category != "static" and u.group}
    out: dict[str, list[LiveUnit]] = {}
    for u in units:
        if u.group in names and u.category != "static":
            out.setdefault(u.group, []).append(u)
    return out


def _spare_slots(
    airport: Airport, aircraft: type[FlyingType], side: int, units: list[LiveUnit]
) -> list[ParkingSlot] | None:
    """The group's slots, one per unit in its order, if it is a spare."""
    out: list[ParkingSlot] = []
    for u in units:
        if (
            u.side != side
            or u.type != aircraft.id
            or u.in_air
            or u.speed >= SPARE_MAX_SPEED
            or u.player is not None
        ):
            return None
        slot = min(
            airport.parking_slots,
            key=lambda s: _distance(u, s),
            default=None,
        )
        if slot is None or not _on(u, slot) or slot in out:
            return None
        out.append(slot)
    return out


def _on(u: LiveUnit, slot: ParkingSlot) -> bool:
    return _distance(u, slot) <= PARKING_RADIUS_M


def _distance(u: LiveUnit, slot: ParkingSlot) -> float:
    return math.hypot(u.x - slot.position.x, u.y - slot.position.y)
