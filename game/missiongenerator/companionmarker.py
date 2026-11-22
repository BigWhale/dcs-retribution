"""The mission marker of a dynamic campaign: one hidden static whose name
carries the uuid of the mission written, so Retribution can tell its own
mission from any other the server runs (`game.companionlink.liverecord`)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dcs import Mission, Point
from dcs.statics import Fortification

from game.companionlink.liverecord import MARKER_PREFIX

if TYPE_CHECKING:
    from game import Game

#: How far inside the map's south-west corner the marker stands.
MARKER_INSET_M = 1000.0


def add_mission_marker(mission: Mission, game: Game, uuid: str) -> None:
    """Adds the marker static `MARKER_PREFIX + uuid`: a small object at the
    map's south-west corner, of the blue coalition's country, hidden on the
    F10 map, the planner and the MFDs."""
    name = MARKER_PREFIX + uuid
    bounds = game.theater.terrain.bounds
    position = Point(
        bounds.bottom + MARKER_INSET_M, bounds.left + MARKER_INSET_M, mission.terrain
    )
    blue = mission.coalition["blue"]
    country = blue.country_by_id(game.blue.faction.country.id) or next(
        iter(blue.countries.values())
    )
    group = mission.static_group(
        country, name, Fortification.Black_Tyre, position, hidden=True
    )
    group.hidden_on_planner = True
    group.hidden_on_mfd = True
    # DCS names a static object by its unit; pydcs adds " object" to it.
    group.units[0].name = name
