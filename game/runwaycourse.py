"""The magnetic course of a runway, for the kneeboard's CRS column.

pydcs knows a runway only by its designator. The true course of each strip
comes from DCS itself, read once per theatre by DCS Companion's hook and kept
in `resources/dcs/runways/<terrain>.json`; the magnetic variation from the
World Magnetic Model at the airfield on the mission's date.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Optional

from pygeomag import GeoMag, decimal_year_from_date  # type: ignore
from pygeomag.wmm.wmm_2010 import WMM_2010  # type: ignore
from pygeomag.wmm.wmm_2015v2 import WMM_2015v2  # type: ignore
from pygeomag.wmm.wmm_2020 import WMM_2020  # type: ignore
from pygeomag.wmm.wmm_2025 import WMM_2025  # type: ignore

RUNWAYS_DIR = Path("resources/dcs/runways")

#: How far a designator may be from the strip's course and still name its end.
END_SLACK_DEG = 30


@cache
def _courses(terrain: str) -> dict[int, list[float]]:
    path = RUNWAYS_DIR / f"{terrain}.json"
    if not path.exists():
        logging.info(f"No runway courses for {terrain}; the kneeboard has no CRS")
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(k): [float(c) for c in v] for k, v in data["airbases"].items()}


def _angle(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def true_course(terrain: str, airbase_id: int, runway_name: str) -> Optional[float]:
    """The true course of the runway end named, or None when not known."""
    try:
        designator = int(runway_name.rstrip("LCR")) * 10
    except ValueError:
        return None
    for course in _courses(terrain).get(airbase_id, []):
        for end in (course, (course + 180) % 360):
            if _angle(end, designator) <= END_SLACK_DEG:
                return end
    return None


@cache
def _model(year: int) -> GeoMag:
    if year < 2015:
        return GeoMag(coefficients_data=WMM_2010)
    if year < 2020:
        return GeoMag(coefficients_data=WMM_2015v2)
    if year < 2025:
        return GeoMag(coefficients_data=WMM_2020)
    return GeoMag(coefficients_data=WMM_2025)


def declination(lat: float, lon: float, when: datetime) -> float:
    """Magnetic variation in degrees, east positive, from the WMM.

    Dates before 2010 or from 2030 on are extrapolated from the nearest model.
    """
    result = _model(when.year).calculate(
        glat=lat,
        glon=lon,
        alt=0,
        time=decimal_year_from_date(when),
        allow_date_outside_lifespan=True,
    )
    return float(result.d)


def magnetic_course(
    terrain: str,
    airbase_id: int,
    runway_name: str,
    lat: float,
    lon: float,
    when: datetime,
) -> Optional[int]:
    """The runway end's magnetic course, whole degrees 1 to 360, or None."""
    course = true_course(terrain, airbase_id, runway_name)
    if course is None:
        return None
    magnetic = round(course - declination(lat, lon, when)) % 360
    return magnetic or 360
