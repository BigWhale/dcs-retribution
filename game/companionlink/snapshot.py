"""A minute snapshot of the running mission, as DCS Companion streams it.

The companion serves each snapshot as one JSON envelope: `minute`, `boot`,
`session`, `modelTime`, `takenAt` and the hook's `campaign_state` answer in
`state` (`meta`, `groups`, `statics`, `airbases`). Only what the map needs is
kept: every unit of every group and every static that still exists, with its
position, motion and side.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("game.companionlink")

#: A group's `cat` as a unit category; anything else (a train) is ground.
CATEGORIES = {0: "plane", 1: "helicopter", 2: "ground", 3: "ship"}


@dataclass(frozen=True)
class LiveUnit:
    name: str
    group: str  # "" for statics
    category: str  # "plane" | "helicopter" | "ground" | "ship" | "static"
    type: str
    side: int  # 0 neutral, 1 red, 2 blue
    x: float  # map metres, north
    y: float  # map metres, east
    alt: float  # metres
    heading: float  # degrees
    speed: float  # m/s
    in_air: bool
    player: Optional[str]


@dataclass(frozen=True)
class LiveSnapshot:
    minute: int
    boot: str
    session: int
    model_time: float
    units: tuple[LiveUnit, ...]  # groups' units, then statics with exists true
    size: int  # bytes of the event's data

    @property
    def unit_count(self) -> int:
        """The units that are not statics."""
        return sum(1 for u in self.units if u.category != "static")


def parse_snapshot(data: str) -> LiveSnapshot:
    """The snapshot in an envelope; ValueError on bad JSON or envelope.

    A group, unit or static that cannot be read is skipped (logged at debug),
    so one odd object never costs the whole minute.
    """
    env = json.loads(data)
    if not isinstance(env, dict):
        raise ValueError("snapshot: not a JSON object")
    minute = _envelope_int(env, "minute")
    boot = env.get("boot")
    if not isinstance(boot, str):
        raise ValueError("snapshot: missing boot")
    session = _envelope_int(env, "session")
    model_time = _number(env.get("modelTime"))
    if model_time is None:
        raise ValueError("snapshot: missing modelTime")
    state = env.get("state")
    if not isinstance(state, dict):
        raise ValueError("snapshot: missing state")

    units: list[LiveUnit] = []
    for g in _list(state, "groups"):
        try:
            units.extend(_group_units(g))
        except (KeyError, TypeError, ValueError) as e:
            log.debug("companion snapshot: group skipped: %s", e)
    for s in _list(state, "statics"):
        try:
            static = _static(s)
        except (KeyError, TypeError, ValueError) as e:
            log.debug("companion snapshot: static skipped: %s", e)
            continue
        if static is not None:
            units.append(static)

    return LiveSnapshot(
        minute=minute,
        boot=boot,
        session=session,
        model_time=model_time,
        units=tuple(units),
        size=len(data.encode("utf-8")),
    )


def _envelope_int(env: dict[str, Any], key: str) -> int:
    v = env.get(key)
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError(f"snapshot: missing {key}")
    return v


def _list(state: dict[str, Any], key: str) -> list[Any]:
    v = state.get(key)
    if v is None:
        return []
    if not isinstance(v, list):
        raise ValueError(f"snapshot: state.{key} is not a list")
    return v


def _number(v: Any) -> Optional[float]:
    """v as a finite float; None for null, a missing field or anything else."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _required(obj: dict[str, Any], key: str) -> float:
    v = _number(obj.get(key))
    if v is None:
        raise ValueError(f"no {key}")
    return v


def _text(obj: dict[str, Any], key: str) -> str:
    v = obj.get(key)
    if not isinstance(v, str):
        raise ValueError(f"no {key}")
    return v


def _side(obj: dict[str, Any]) -> int:
    v = obj.get("side")
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _group_units(g: Any) -> list[LiveUnit]:
    if not isinstance(g, dict):
        raise ValueError("group is not an object")
    name = _text(g, "name")
    cat = g.get("cat")
    category = CATEGORIES.get(cat, "ground") if isinstance(cat, int) else "ground"
    side = _side(g)
    units = g.get("units") or []
    if not isinstance(units, list):
        raise ValueError(f"group {name}: units is not a list")
    out: list[LiveUnit] = []
    for u in units:
        try:
            if not isinstance(u, dict):
                raise ValueError("unit is not an object")
            player = u.get("player")
            out.append(
                LiveUnit(
                    name=_text(u, "name"),
                    group=name,
                    category=category,
                    type=_text(u, "type"),
                    side=side,
                    x=_required(u, "x"),
                    y=_required(u, "y"),
                    alt=_number(u.get("alt")) or 0.0,
                    heading=_number(u.get("hdg")) or 0.0,
                    speed=_number(u.get("spd")) or 0.0,
                    in_air=u.get("inAir") is True,
                    player=player if isinstance(player, str) and player else None,
                )
            )
        except (KeyError, TypeError, ValueError) as e:
            log.debug("companion snapshot: unit of group %s skipped: %s", name, e)
    return out


def _static(s: Any) -> Optional[LiveUnit]:
    if not isinstance(s, dict):
        raise ValueError("static is not an object")
    if s.get("exists") is not True:
        return None
    return LiveUnit(
        name=_text(s, "name"),
        group="",
        category="static",
        type=_text(s, "type"),
        side=_side(s),
        x=_required(s, "x"),
        y=_required(s, "y"),
        alt=_number(s.get("alt")) or 0.0,
        heading=_number(s.get("hdg")) or 0.0,
        speed=0.0,
        in_air=False,
        player=None,
    )
