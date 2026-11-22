"""Parking spots for a Cold or Warm launch from an airfield.

Against the real Persian Gulf terrain: Al Dhafra's slots, with the live units
of a snapshot placed on them as DCS Companion reports them (map metres, `x`
north and `y` east, the frame of pydcs's `Point`).
"""

from __future__ import annotations

from typing import Optional

import pytest
from dcs.planes import F_16C_50, FA_18C_hornet
from dcs.terrain import Airport, PersianGulf
from dcs.terrain.terrain import ParkingSlot

from game.companionlink.parking import ParkingClaims, ParkingPlan, plan_parking
from game.companionlink.snapshot import LiveSnapshot, LiveUnit

BLUE = 2
RED = 1


@pytest.fixture
def airport() -> Airport:
    return PersianGulf().airports["Al Dhafra AFB"]


def unit(
    group: str,
    slot: ParkingSlot,
    type: str = F_16C_50.id,
    side: int = BLUE,
    speed: float = 0.0,
    player: Optional[str] = None,
    n: int = 1,
) -> LiveUnit:
    return LiveUnit(
        name=f"{group}-{n}",
        group=group,
        category="plane",
        type=type,
        side=side,
        x=slot.position.x + 2.0,
        y=slot.position.y - 3.0,
        alt=20.0,
        heading=90.0,
        speed=speed,
        in_air=False,
        player=player,
    )


def static(name: str, slot: ParkingSlot) -> LiveUnit:
    return LiveUnit(
        name=name,
        group="",
        category="static",
        type="F-16C_50",
        side=BLUE,
        x=slot.position.x,
        y=slot.position.y,
        alt=20.0,
        heading=0.0,
        speed=0.0,
        in_air=False,
        player=None,
    )


def snapshot(units: list[LiveUnit], model_time: float = 180.0) -> LiveSnapshot:
    return LiveSnapshot(
        minute=3,
        boot="boot-1",
        session=1,
        model_time=model_time,
        units=tuple(units),
        size=0,
    )


def free(airport: Airport) -> list[ParkingSlot]:
    """Al Dhafra's slots for an F-16C, in pydcs's order, before anything."""
    return airport.free_parking_slots(F_16C_50)


def test_spares_first_then_free_slots(airport: Airport) -> None:
    slots = free(airport)
    a, b = slots[10], slots[11]
    big = slots[20:24]
    # A slot an earlier generator run marked as used: the live mission says it
    # is free, and the mark is still there afterwards.
    slots[0].unit_id = 99
    units = [unit("Spare A", a)]
    units += [unit("Big", s, n=i) for i, s in enumerate(big, start=1)]
    units += [unit("Spare B", b)]

    plan = plan_parking(
        airport, F_16C_50, 4, BLUE, snapshot(units), (), ParkingClaims()
    )

    assert plan == ParkingPlan(
        airport_id=airport.id,
        slots=[a, b, slots[0], slots[1]],
        despawn=["Spare A", "Spare B"],
    )
    assert slots[0].unit_id == 99
    assert all(s.unit_id is None for s in airport.parking_slots if s is not slots[0])


def test_only_spares_are_taken(airport: Airport) -> None:
    slots = free(airport)
    taken = slots[0:6]
    units = [
        unit("Uzi 1|2|1|F-16C_50|", taken[0]),
        unit("Player", taken[1], player="Kilo"),
        unit("Moving", taken[2], speed=3.0),
        unit("Red", taken[3], side=RED),
        unit("Hornet", taken[4], type=FA_18C_hornet.id),
        unit("Half on the ramp", taken[5], n=1),
        LiveUnit(
            name="Half on the ramp-2",
            group="Half on the ramp",
            category="plane",
            type=F_16C_50.id,
            side=BLUE,
            x=airport.position.x,
            y=airport.position.y,
            alt=20.0,
            heading=0.0,
            speed=0.0,
            in_air=False,
            player=None,
        ),
    ]

    plan = plan_parking(
        airport,
        F_16C_50,
        2,
        BLUE,
        snapshot(units),
        {"Uzi 1|2|1|F-16C_50|"},
        ParkingClaims(),
    )

    assert plan == ParkingPlan(
        airport_id=airport.id, slots=[slots[6], slots[7]], despawn=[]
    )


def test_occupied_slots_not_free(airport: Airport) -> None:
    slots = free(airport)
    c, d = slots[0], slots[1]
    units = [static("Tent", c), unit("Hornet", d, type=FA_18C_hornet.id)]

    plan = plan_parking(
        airport, F_16C_50, 2, BLUE, snapshot(units), (), ParkingClaims()
    )

    assert plan is not None
    assert plan.slots == [slots[2], slots[3]]
    assert c not in plan.slots and d not in plan.slots


def test_claimed_spots_and_spent_spares_skipped(airport: Airport) -> None:
    slots = free(airport)
    a, b = slots[10], slots[11]
    units = [unit("Spare A", a), unit("Spare B", b)]
    claims = ParkingClaims()

    first = plan_parking(airport, F_16C_50, 3, BLUE, snapshot(units), (), claims)
    assert first is not None
    assert first.slots == [a, b, slots[0]]
    claims.claim(first, "Uzi 1")

    second = plan_parking(airport, F_16C_50, 3, BLUE, snapshot(units), (), claims)
    assert second is not None
    assert second.despawn == []
    assert second.slots == [slots[1], slots[2], slots[3]]
    assert not {s.crossroad_idx for s in first.slots} & {
        s.crossroad_idx for s in second.slots
    }

    # The spawn is confirmed late: newer snapshots still show the spares, and
    # the claim holds while the launch is on its way, then up to the model
    # time it is released with.
    on_its_way = plan_parking(
        airport, F_16C_50, 3, BLUE, snapshot(units, model_time=500.0), (), claims
    )
    assert on_its_way == second
    claims.release("Uzi 1", 560.0)
    released = plan_parking(
        airport, F_16C_50, 3, BLUE, snapshot(units, model_time=560.0), (), claims
    )
    assert released == second

    later = plan_parking(
        airport, F_16C_50, 3, BLUE, snapshot(units, model_time=561.0), (), claims
    )
    assert later == first


def test_not_enough_spots_is_none(airport: Airport) -> None:
    slots = free(airport)
    units = [unit("Spare A", slots[0])]

    plan = plan_parking(
        airport,
        F_16C_50,
        len(slots) + 1,
        BLUE,
        snapshot(units),
        (),
        ParkingClaims(),
    )

    assert plan is None
