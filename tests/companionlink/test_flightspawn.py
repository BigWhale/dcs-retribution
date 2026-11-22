"""A flight's group made ready for the live mission.

The table is a real pydcs group (`Group.dict()`) of a fresh Persian Gulf
mission, as Retribution's generator writes one: an F-16C pair named in
Retribution's pattern, one unit a Client, a waypoint with an ETA and an orbit
that stops at a mission time.
"""

from __future__ import annotations

import json
from typing import Any

from dcs import Mission, task
from dcs.countries import USA
from dcs.mapping import Point
from dcs.planes import F_16C_50
from dcs.terrain import PersianGulf
from dcs.unit import Skill

from game.companionlink.flightspawn import IdAllocator, prepare_spawn
from game.companionlink.snapshot import LiveSnapshot

NAME = "Uzi 1|2|1|F-16C_50|"


def make_table() -> dict[str, Any]:
    mission = Mission(terrain=PersianGulf())
    group = mission.flight_group(
        country=mission.country(USA.name),
        name=NAME,
        aircraft_type=F_16C_50,
        airport=None,
        position=Point(0, 0, mission.terrain),
        altitude=6000,
        group_size=2,
    )
    group.units[0].skill = Skill.Client
    hold = group.add_waypoint(Point(20000, 0, mission.terrain), 6000)
    hold.ETA = 120
    orbit = task.ControlledTask(task.OrbitAction(6000, 500))
    orbit.stop_after_time(900)
    hold.tasks.append(orbit)
    strike = group.add_waypoint(Point(40000, 0, mission.terrain), 6000)
    strike.tasks.append(task.AttackGroup(311))
    return group.dict()


def snapshot(
    max_group_id: int = 466,
    max_unit_id: int = 809,
    names: frozenset[str] = frozenset(),
) -> LiveSnapshot:
    return LiveSnapshot(
        minute=3,
        boot="boot-1",
        session=1,
        model_time=180.0,
        units=(),
        size=0,
        max_group_id=max_group_id,
        max_unit_id=max_unit_id,
        names=names,
    )


def spawn(
    table: dict[str, Any],
    snap: LiveSnapshot,
    ids: IdAllocator,
    sent: set[str],
    model_time: float = 1000.0,
    held: frozenset[str] = frozenset(),
) -> Any:
    return prepare_spawn(
        table,
        country=2,
        category="plane",
        snapshot=snap,
        ids=ids,
        sent_names=sent,
        held_names=held,
        model_time=model_time,
    )


def units(group: dict[str, Any]) -> list[dict[str, Any]]:
    u = group["units"]
    return list(u.values()) if isinstance(u, dict) else list(u)


def points(group: dict[str, Any]) -> list[dict[str, Any]]:
    p = group["route"]["points"]
    return list(p.values()) if isinstance(p, dict) else list(p)


def tasks(point: dict[str, Any]) -> list[dict[str, Any]]:
    t = point["task"]["params"]["tasks"]
    return list(t.values()) if isinstance(t, dict) else list(t)


def test_ids_above_snapshot_and_gap() -> None:
    request = spawn(make_table(), snapshot(), IdAllocator(), set())

    assert request.group["groupId"] >= 1466
    unit_ids = [u["unitId"] for u in units(request.group)]
    assert all(i >= 1809 for i in unit_ids)
    assert len(set(unit_ids)) == 2


def test_ids_never_repeat_within_a_mission() -> None:
    ids, sent, snap = IdAllocator(), set[str](), snapshot()
    first = spawn(make_table(), snap, ids, sent)
    second = spawn(make_table(), snap, ids, sent)

    assert first.group["groupId"] != second.group["groupId"]
    first_units = {u["unitId"] for u in units(first.group)}
    second_units = {u["unitId"] for u in units(second.group)}
    assert not first_units & second_units


def test_self_references_follow_new_ids() -> None:
    request = spawn(make_table(), snapshot(), IdAllocator(), set())
    group = request.group

    eplrs = [
        t["params"]["action"]["params"]
        for p in points(group)
        for t in tasks(p)
        if t["id"] == "WrappedAction" and t["params"]["action"]["id"] == "EPLRS"
    ]
    assert eplrs and all(e["groupId"] == group["groupId"] for e in eplrs)
    attack = [t for p in points(group) for t in tasks(p) if t["id"] == "AttackGroup"]
    assert [t["params"]["groupId"] for t in attack] == [311]


def test_name_taken_picks_next_number() -> None:
    ids, sent = IdAllocator(), set[str]()
    snap = snapshot(names=frozenset({NAME}))

    request = spawn(make_table(), snap, ids, sent)

    assert request.name == "Uzi 1|2|2|F-16C_50|"
    assert request.group["name"] == request.name
    assert [u["name"] for u in units(request.group)] == [
        "Uzi 1|2|2|F-16C_50| Pilot #1",
        "Uzi 1|2|2|F-16C_50| Pilot #2",
    ]
    assert request.name in sent
    assert spawn(make_table(), snap, ids, sent).name == "Uzi 1|2|3|F-16C_50|"


def test_name_held_by_the_companion_picks_next_number() -> None:
    """A name the companion's spawns hold is taken, though not in the snapshot."""
    request = spawn(
        make_table(), snapshot(), IdAllocator(), set(), held=frozenset({NAME})
    )

    assert request.name == "Uzi 1|2|2|F-16C_50|"
    assert request.group["name"] == request.name


def test_skill_and_times() -> None:
    request = spawn(make_table(), snapshot(), IdAllocator(), set(), model_time=1000)
    group = request.group

    assert all(u["skill"] not in ("Client", "Player") for u in units(group))
    hold = points(group)[1]
    assert hold["ETA"] == 1120
    orbits = [t for t in tasks(hold) if t["id"] == "ControlledTask"]
    assert [t["params"]["stopCondition"]["time"] for t in orbits] == [1900]


def test_body_is_json() -> None:
    request = spawn(make_table(), snapshot(), IdAllocator(), set())

    body = json.loads(request.body())
    assert body["group"]["name"] == request.name
    assert body["country"] == 2 and body["category"] == "plane"
