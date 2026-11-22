from __future__ import annotations

import logging
from functools import cache
from typing import TYPE_CHECKING

from dcs import Point
from dcs.planes import plane_map
from dcs.ships import ship_map
from dcs.vehicles import vehicle_map
from pydantic import BaseModel

from game.ato.flighttype import FlightType
from game.data.units import ANTI_AIR_UNIT_CLASSES, UnitClass
from game.dcs.aircrafttype import AircraftType
from game.dcs.groundunittype import GroundUnitType
from game.dcs.shipunittype import ShipUnitType
from game.server.leaflet import LeafletPoint
from game.sidc import (
    AirEntity,
    Entity,
    LandUnitEntity,
    SeaSurfaceEntity,
    StandardIdentity,
    SymbolIdentificationCode,
    SymbolSet,
    UnknownEntity,
)

if TYPE_CHECKING:
    from game.companionlink.snapshot import LiveSnapshot, LiveUnit
    from game.theater import ConflictTheater


_IDENTITY_BY_SIDE = {
    2: StandardIdentity.FRIEND,
    1: StandardIdentity.HOSTILE_FAKER,  # APP-6 "hostile/faker", as flights use
    0: StandardIdentity.UNKNOWN,
}

# Aircraft whose top task says what they are, whatever their role text says.
_AIR_ENTITY_BY_TOP_TASK = {
    FlightType.AEWC: AirEntity.AIRBORNE_EARLY_WARNING,
    FlightType.REFUELING: AirEntity.TANKER,
    FlightType.TRANSPORT: AirEntity.CARGO,
}

# Keywords of an aircraft type's role text ("Multirole Fighter", "Close Air
# Support/Attack"), tried in order on each part of the role in turn.
_AIR_ENTITY_BY_ROLE_WORD = [
    ("aew", AirEntity.AIRBORNE_EARLY_WARNING),
    ("tanker", AirEntity.TANKER),
    ("refuel", AirEntity.TANKER),
    ("electronic", AirEntity.ELECTRONIC_COMBAT_JAMMER),
    ("fighter-bomber", AirEntity.FIGHTER_BOMBER),
    ("fighter bomber", AirEntity.FIGHTER_BOMBER),
    ("strike fighter", AirEntity.FIGHTER_BOMBER),
    ("bomber", AirEntity.BOMBER),
    ("fighter", AirEntity.FIGHTER),
    ("interceptor", AirEntity.FIGHTER),
    ("attack", AirEntity.ATTACK_STRIKE),
    ("strike", AirEntity.ATTACK_STRIKE),
    ("close air support", AirEntity.ATTACK_STRIKE),
    ("transport", AirEntity.CARGO),
    ("cargo", AirEntity.CARGO),
    ("recon", AirEntity.RECONNAISSANCE),
    ("observation", AirEntity.RECONNAISSANCE),
    ("patrol", AirEntity.PATROL),
]

_ARMOR_CLASSES = {UnitClass.TANK, UnitClass.IFV, UnitClass.APC}


def _plane_entity(type_name: str) -> Entity:
    aircraft = next(AircraftType.for_dcs_type(plane_map[type_name]))
    if aircraft.task_priorities:
        top = max(aircraft.task_priorities, key=aircraft.task_priorities.__getitem__)
        if top in _AIR_ENTITY_BY_TOP_TASK:
            return _AIR_ENTITY_BY_TOP_TASK[top]
    for part in aircraft.role.lower().replace(",", "/").split("/"):
        for word, entity in _AIR_ENTITY_BY_ROLE_WORD:
            if word in part:
                return entity
    return AirEntity.UNSPECIFIED


def _ground_entity(type_name: str) -> Entity:
    unit_class = next(GroundUnitType.for_dcs_type(vehicle_map[type_name])).unit_class
    if unit_class in ANTI_AIR_UNIT_CLASSES:
        return LandUnitEntity.AIR_DEFENSE
    if unit_class in _ARMOR_CLASSES:
        return LandUnitEntity.ARMOR_ARMORED_MECHANIZED_SELF_PROPELLED_TRACKED
    if unit_class is UnitClass.MISSILE:
        return LandUnitEntity.MISSILE
    return LandUnitEntity.UNSPECIFIED


def _ship_entity(type_name: str) -> Entity:
    unit_class = next(ShipUnitType.for_dcs_type(ship_map[type_name])).unit_class
    if unit_class is UnitClass.AIRCRAFT_CARRIER:
        return SeaSurfaceEntity.CARRIER
    if unit_class is UnitClass.HELICOPTER_CARRIER:
        # As Retribution draws its own LHA control points.
        return SeaSurfaceEntity.AMPHIBIOUS_ASSAULT_SHIP_GENERAL
    return SeaSurfaceEntity.SURFACE_COMBATANT_LINE


@cache
def _symbol_set_and_entity(category: str, type_name: str) -> tuple[SymbolSet, Entity]:
    """The symbol set and entity of a DCS unit type; the generic symbol of its
    dimension when the type tables do not know it. Cached: a snapshot holds
    hundreds of units of a few dozen types."""
    if category == "helicopter":
        return SymbolSet.AIR, AirEntity.ROTARY_WING
    lookups = {
        "plane": (SymbolSet.AIR, AirEntity.UNSPECIFIED, _plane_entity),
        "ground": (SymbolSet.LAND_UNIT, LandUnitEntity.UNSPECIFIED, _ground_entity),
        "ship": (SymbolSet.SEA_SURFACE, SeaSurfaceEntity.UNSPECIFIED, _ship_entity),
    }
    if category not in lookups:
        return SymbolSet.UNKNOWN, UnknownEntity.UNSPECIFIED
    symbol_set, generic, entity_of = lookups[category]
    try:
        return symbol_set, entity_of(type_name)
    except Exception:  # an unknown type, or no data for it: the generic symbol
        logging.debug("No symbol for live %s type %s", category, type_name)
        return symbol_set, generic


def live_unit_sidc(unit: LiveUnit) -> SymbolIdentificationCode:
    """The NATO symbol code of a live unit: its side's affiliation, its
    category's dimension and, where Retribution knows its type, its function."""
    symbol_set, entity = _symbol_set_and_entity(unit.category, unit.type)
    return SymbolIdentificationCode(
        standard_identity=_IDENTITY_BY_SIDE.get(unit.side, StandardIdentity.UNKNOWN),
        symbol_set=symbol_set,
        entity=entity,
    )


class LiveUnitJs(BaseModel):
    name: str
    group: str
    category: str
    type: str
    side: int
    position: LeafletPoint
    altitude: float  # metres
    heading: float
    speed: float  # m/s
    in_air: bool
    player: str | None
    sidc: str  # NATO symbol code (APP-6), see live_unit_sidc

    class Config:
        title = "LiveUnit"

    @staticmethod
    def all_for(
        snapshot: LiveSnapshot | None, theater: ConflictTheater
    ) -> list[LiveUnitJs]:
        """Every unit of the snapshot with its position on the map; [] for None."""
        if snapshot is None:
            return []
        terrain = theater.terrain
        return [
            LiveUnitJs(
                name=u.name,
                group=u.group,
                category=u.category,
                type=u.type,
                side=u.side,
                position=LeafletPoint.from_latlng(Point(u.x, u.y, terrain).latlng()),
                altitude=u.alt,
                heading=u.heading,
                speed=u.speed,
                in_air=u.in_air,
                player=u.player,
                sidc=str(live_unit_sidc(u)),
            )
            for u in snapshot.units
        ]
