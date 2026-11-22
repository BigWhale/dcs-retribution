"""A flight's group made for the live mission of a dynamic campaign.

`generate_flight_table` runs Retribution's mission generator at the mission
clock's time into a throwaway mission that holds only the flight's package,
with the flight in the start type it is launched with (on given parking slots
for a Cold or Warm start), and lifts the flight's pydcs group table out
(`Group.dict()`). `prepare_spawn` then makes that table fit the running
mission: new group and unit ids, a name the mission does not use, AI skills
and mission times turned into the live mission's model times.
The result is the `POST /v1/spawn` request for DCS Companion; `unit_names`
gives its unit names, which a confirmed launch adds to the name map.

Only a dynamic campaign's launcher calls these.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable, Collection, Iterable
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dcs.task import TargetType
from dcs.terrain import ParkingSlot
from dcs.unitgroup import FlyingGroup

from game.ato.flightstate import Uninitialized, WaitingForStart
from game.ato.starttype import StartType
from game.companionlink.commands import SpawnRequest
from game.companionlink.snapshot import LiveSnapshot

if TYPE_CHECKING:
    from game import Game
    from game.ato.flight import Flight
    from game.missiongenerator import MissionGenerator

#: Retribution's group name: `<flight>|<country>|<n>|<type>|`.
_NAME = re.compile(r"^(?P<head>.*\|\d+\|)(?P<n>\d+)(?P<tail>\|[^|]*\|)$")

#: Skills a spawned group cannot have: nobody sits in its cockpits.
_HUMAN_SKILLS = ("Client", "Player")
_AI_SKILL = "High"


class IdAllocator:
    """Group and unit ids for the groups launched into one mission.

    One per mission (the snapshot's boot and session): a new mission gets a
    new allocator. Every id is above both the newest snapshot's highest and
    the highest this allocator handed out, plus `GAP`, since the snapshot can
    be a minute old and other spawns may have taken ids since.
    """

    GAP = 1000

    def __init__(self) -> None:
        self._group = 0
        self._unit = 0

    def take(self, snapshot: LiveSnapshot, units: int) -> tuple[int, list[int]]:
        """A group id and `units` unit ids, none of them handed out before."""
        group = max(snapshot.max_group_id, self._group) + self.GAP + 1
        first = max(snapshot.max_unit_id, self._unit) + self.GAP + 1
        unit_ids = list(range(first, first + units))
        self._group = group
        if unit_ids:
            self._unit = unit_ids[-1]
        return group, unit_ids


def generate_flight_table(
    game: Game,
    flight: Flight,
    now: datetime,
    start_type: StartType = StartType.IN_FLIGHT,
    parking: list[ParkingSlot] | None = None,
) -> tuple[dict[str, Any], int, str]:
    """The flight's group table, its country id and "plane" or "helicopter".

    Retribution's generator runs at `now` into a temporary .miz, its ATOs
    holding only the flight's package for the time, with the flight in
    `start_type` due at `now`, and without the parked untasked aircraft. A
    Cold or Warm flight from an airfield stands on `parking` (one slot per
    aircraft) when given, else on the generator's own choice. The ATOs, those
    two settings and the package's flights' start types, states and group ids
    are as they were afterwards, and the flight's `parking_override` is None.
    The table has no late activation and is not uncontrolled: the companion
    spawns it when asked and it starts up at once.
    """
    from game.missiongenerator import MissionGenerator

    package = flight.package
    for f in package.flights:
        if not f.squadron.location.runway_is_operational():
            # The generator would skip the flight and send its pilots home.
            raise RuntimeError(
                f"{f} cannot be generated: the runway at "
                f"{f.squadron.location.name} is not operational"
            )

    atos = [game.blue.ato, game.red.ato]
    saved_packages = [ato.packages for ato in atos]
    saved_flights = [(f, f.start_type, f.state, f.group_id) for f in package.flights]
    settings = game.settings
    saved_untasked = (
        settings.perf_disable_untasked_blufor_aircraft,
        settings.perf_disable_untasked_opfor_aircraft,
    )
    try:
        # Parked untasked aircraft claim a pilot each, given back only when a
        # turn begins, which a dynamic campaign never does: leave them out.
        settings.perf_disable_untasked_blufor_aircraft = True
        settings.perf_disable_untasked_opfor_aircraft = True
        for ato in atos:
            ato.packages = [package] if package in ato.packages else []
        if not any(ato.packages for ato in atos):
            raise RuntimeError(f"{flight} is in neither coalition's ATO")
        for f in package.flights:
            if f is flight:
                continue
            if isinstance(f.state, Uninitialized):
                f.state.reinitialize(now)
        flight.start_type = start_type
        flight.parking_override = parking
        flight.set_state(WaitingForStart(flight, flight.squadron.settings, now))
        flight.group_id = 0

        generator = MissionGenerator(game, now)
        with tempfile.TemporaryDirectory(prefix="retribution-launch-") as tmp:
            generator.generate_miz(Path(tmp) / "launch.miz")

        group = generator.mission.find_group_by_id(flight.group_id)
        if flight.group_id <= 0 or not isinstance(group, FlyingGroup):
            raise RuntimeError(f"{flight} was not generated")
        country, category = _country_and_category(generator, group)
        table = group.dict()
    finally:
        (
            settings.perf_disable_untasked_blufor_aircraft,
            settings.perf_disable_untasked_opfor_aircraft,
        ) = saved_untasked
        for ato, packages in zip(atos, saved_packages):
            ato.packages = packages
        for f, saved_type, state, group_id in saved_flights:
            f.start_type = saved_type
            f.set_state(state)
            f.group_id = group_id
        flight.parking_override = None

    table.pop("lateActivation", None)
    table.pop("uncontrolled", None)
    return table, country, category


def _country_and_category(
    generator: MissionGenerator, group: FlyingGroup[Any]
) -> tuple[int, str]:
    for coalition in generator.mission.coalition.values():
        for country in coalition.countries.values():
            if group in country.helicopter_group:
                return country.id, "helicopter"
            if group in country.plane_group:
                return country.id, "plane"
    raise RuntimeError(f"group {group.name} is in no country")


def prepare_spawn(
    table: dict[str, Any],
    *,
    country: int,
    category: str,
    snapshot: LiveSnapshot,
    ids: IdAllocator,
    sent_names: set[str],
    model_time: float,
    held_names: Collection[str] = frozenset(),
) -> SpawnRequest:
    """The spawn request for a generated group table, at once (no `at`).

    The table is copied, never changed. In the copy:
    - the group and its units get new ids (`ids`); references to the group's
      own id and its units' ids follow them, references to anything else stay;
    - the group gets a name used neither by the snapshot, nor by `sent_names`,
      nor by `held_names` (the names the companion's spawns hold, which it
      refuses to spawn again even when the group is gone from the mission;
      Retribution's `<n>` raised until the name and its unit names are free),
      in every string that named it; the name is added to `sent_names`;
    - a Client or Player skill becomes an AI skill;
    - every waypoint ETA and every task condition's time move by `model_time`,
      from the generated mission's times to the live mission's.
    """
    group = _plain(table)

    units = _units(group)
    old_group = group["groupId"]
    new_group, new_units = ids.take(snapshot, len(units))
    unit_map = {u["unitId"]: new for u, new in zip(units, new_units)}

    def reid(container: Any, key: Any, value: Any) -> None:
        if key == "groupId" and value == old_group:
            container[key] = new_group
        elif key in ("unitId", "missionUnitId") and value in unit_map:
            container[key] = unit_map[value]

    _walk(group, reid)

    old_name = group["name"]
    name = _free_name(
        old_name,
        [u["name"] for u in units],
        snapshot.names | sent_names | frozenset(held_names),
    )
    if name != old_name:

        def rename(container: Any, key: Any, value: Any) -> None:
            if isinstance(value, str) and old_name in value:
                container[key] = value.replace(old_name, name)

        _walk(group, rename)

    for u in units:
        if u.get("skill") in _HUMAN_SKILLS:
            u["skill"] = _AI_SKILL

    for point in _values(group.get("route", {}).get("points", [])):
        if _is_number(point.get("ETA")):
            point["ETA"] += model_time

    def shift(container: Any, key: Any, value: Any) -> None:
        if (
            key in ("condition", "stopCondition")
            and isinstance(value, dict)
            and _is_number(value.get("time"))
        ):
            value["time"] += model_time

    _walk(group, shift)

    sent_names.add(name)
    return SpawnRequest(name=name, country=country, category=category, group=group)


def unit_names(request: SpawnRequest) -> list[str]:
    """The names of the request's units, in the group table's unit order."""
    return [u["name"] for u in _units(request.group)]


def _free_name(name: str, unit_names: list[str], taken: frozenset[str]) -> str:
    """`name`, or its next number whose group and unit names are all free."""
    m = _NAME.match(name)
    first = int(m.group("n")) if m else 1
    for n in range(first, first + 10000):
        if m:
            candidate = f"{m.group('head')}{n}{m.group('tail')}"
        else:
            candidate = name if n == 1 else f"{name} #{n}"
        names = [candidate] + [u.replace(name, candidate) for u in unit_names]
        if not any(x in taken for x in names):
            return candidate
    raise RuntimeError(f"no free name for {name}")


def _plain(v: Any) -> Any:
    """A copy of a pydcs table with JSON values only.

    pydcs leaves a few Python objects in `Group.dict()` that its Lua writer
    turns into text (an `EngageTargets` task's target types); they become the
    same text here.
    """
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, Enum):
        return _plain(v.value)
    if isinstance(v, TargetType):
        return v.id
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    raise TypeError(f"a {type(v).__name__} in a group table: {v!r}")


def _walk(v: Any, fn: Callable[[Any, Any, Any], None]) -> None:
    """Calls fn(container, key, value) for every field at any depth."""
    items: Iterable[tuple[Any, Any]] = (
        v.items() if isinstance(v, dict) else enumerate(v)
    )
    for k, x in list(items):
        if isinstance(x, (dict, list)):
            _walk(x, fn)
        fn(v, k, x)


def _values(v: Any) -> list[Any]:
    """The values of a pydcs list, which is a dict keyed 1..n or a list."""
    if isinstance(v, dict):
        return list(v.values())
    return list(v) if isinstance(v, list) else []


def _units(group: dict[str, Any]) -> list[dict[str, Any]]:
    return [u for u in _values(group.get("units")) if isinstance(u, dict)]


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)
