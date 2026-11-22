"""The name map: a dynamic campaign's `UnitMap`, kept in the saved game.

A turn's `UnitMap` lives only while its mission is generated and debriefed,
and holds pydcs objects that point into the mission. A dynamic campaign plays
one mission for as long as it runs, so it keeps the map in the `Game` and
debriefs live losses against it. `LiveUnitMap` is the same map without pydcs:
plain `str` keys, a theater unit's mapping without its pydcs unit (used only
while generating), and a scenery zone as a `ZoneShape` (a turn's debrief reads
only its `name`). Launches add their units as the companion names them.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Optional, Sequence, TYPE_CHECKING, cast

from dcs.triggers import TriggerZone, TriggerZoneQuadPoint
from dcs.unit import Unit
from shapely.geometry import Point as ShapelyPoint, Polygon as ShapelyPolygon

from game.ato.flight import Flight
from game.unitmap import (
    AirliftUnits,
    FlyingUnit,
    SceneryObjectMapping,
    TheaterUnitMapping,
    UnitMap,
)

if TYPE_CHECKING:
    from game import Game

#: How far from a scenery circle's centre an object still counts as in it:
#: DCS reports an object's origin, which need not be the zone's centre.
SCENERY_SLACK_M = 30.0


@dataclass(frozen=True)
class ZoneShape:
    """A scenery target's trigger zone, in map metres (x north, y east).

    Stands in for the pydcs `TriggerZone` of a `SceneryObjectMapping`: a
    circle of `radius` around (x, y), or the quad of `corners`.
    """

    name: str
    x: float
    y: float
    radius: float
    corners: Optional[tuple[tuple[float, float], ...]] = None


class LiveUnitMap(UnitMap):
    """A `UnitMap` that pickles without pydcs; `Debriefing` reads it as is."""

    @classmethod
    def from_generation(cls, unit_map: UnitMap, game: Game) -> LiveUnitMap:
        """The map a mission's generation made, stripped of pydcs.

        An aircraft whose flight is in neither ATO is a parked aircraft's
        stand-in flight: its pilot was taken away after it was mapped, so it
        is kept without one.
        """
        planned = {
            id(f)
            for coalition in (game.blue, game.red)
            for package in coalition.ato.packages
            for f in package.flights
        }
        m = cls()
        for name, unit in unit_map.aircraft.items():
            pilot = unit.pilot if id(unit.flight) in planned else None
            m.aircraft[str(name)] = FlyingUnit(unit.flight, pilot)
        m.airfields = {str(k): v for k, v in unit_map.airfields.items()}
        m.front_line_units = {str(k): v for k, v in unit_map.front_line_units.items()}
        m.motorpool_units = {str(k): v for k, v in unit_map.motorpool_units.items()}
        for name, mapping in unit_map.theater_objects.items():
            m.theater_objects[str(name)] = TheaterUnitMapping(
                mapping.theater_unit, cast(Unit, None)
            )
        for name, scenery in unit_map.scenery_objects.items():
            m.scenery_objects[str(name)] = SceneryObjectMapping(
                scenery.ground_unit,
                cast(TriggerZone, _shape(scenery.trigger_zone)),
            )
        m.convoys = {str(k): v for k, v in unit_map.convoys.items()}
        m.cargo_ships = {str(k): v for k, v in unit_map.cargo_ships.items()}
        m.airlifts = {str(k): v for k, v in unit_map.airlifts.items()}
        return m

    def add_launched(self, flight: Flight, unit_names: Sequence[str]) -> None:
        """A launched flight's units, by the names the companion spawned.

        Zipped with the roster as `UnitMap.add_aircraft` zips the group's
        units, airlift cargo included; a name already mapped is replaced.
        """
        for pilot, name in zip(flight.roster.iter_pilots(), unit_names):
            self.aircraft[name] = FlyingUnit(flight, pilot)
        transfer = flight.cargo
        if transfer is None or not unit_names:
            return
        capacity_each = math.ceil(transfer.size / len(unit_names))
        for idx, name in enumerate(unit_names):
            base_idx = idx * capacity_each
            cargo = tuple(
                itertools.islice(
                    transfer.iter_units(), base_idx, base_idx + capacity_each
                )
            )
            self.airlifts[name] = AirliftUnits(cargo, transfer)

    def flight_units(self, flight: Flight) -> list[str]:
        """The unit names mapped to `flight`."""
        return [name for name, u in self.aircraft.items() if u.flight is flight]

    def scenery_at(self, x: float, y: float) -> Optional[str]:
        """The scenery zone that holds a point (map metres), by name.

        A quad holds the points inside it; otherwise the nearest circle centre
        within its radius or `SCENERY_SLACK_M`, whichever is larger. None when
        no zone holds it.
        """
        point = ShapelyPoint(x, y)
        nearest: Optional[str] = None
        nearest_d = math.inf
        for mapping in self.scenery_objects.values():
            zone = cast(ZoneShape, mapping.trigger_zone)
            if zone.corners is not None:
                if ShapelyPolygon(zone.corners).covers(point):
                    return zone.name
                continue
            d = math.hypot(zone.x - x, zone.y - y)
            if d <= max(zone.radius, SCENERY_SLACK_M) and d < nearest_d:
                nearest, nearest_d = zone.name, d
        return nearest


def _shape(zone: TriggerZone) -> ZoneShape:
    corners = None
    if isinstance(zone, TriggerZoneQuadPoint):
        corners = tuple((float(v.x), float(v.y)) for v in zone.verticies)
    return ZoneShape(
        name=str(zone.name),
        x=float(zone.position.x),
        y=float(zone.position.y),
        radius=float(zone.radius),
        corners=corners,
    )
