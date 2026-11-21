from __future__ import annotations

from typing import TYPE_CHECKING

from dcs import Point
from pydantic import BaseModel

from game.server.leaflet import LeafletPoint

if TYPE_CHECKING:
    from game.companionlink.snapshot import LiveSnapshot
    from game.theater import ConflictTheater


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
            )
            for u in snapshot.units
        ]
