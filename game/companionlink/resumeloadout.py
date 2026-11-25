"""A resumed aircraft's loadout: its planned loadout stripped down to what the
tick says it still carries.

The tick gives each airborne unit `ammo`, a list of (DCS weapon type, count),
and `fuel`, a fraction of internal fuel (over 1 with fuel in external tanks).
`resume_loadout` matches the tick's weapon types to the planned pylons and
empties the pylons of what was fired. When the tick cannot be matched, the
aircraft keeps its planned weapons and the result says why.

The rules, checked against real ticks (`tests/companionlink/data/
resume_ammo.json`):

- Guns are left out: a type starting `weapons.shells.` is ignored (guns come
  back full).
- A type's *key* is the part of its name after the last `.`, upper-cased,
  letters and digits only (`weapons.missiles.AIM_120C` gives `AIM120C`; the
  tick also names some types without a prefix, `AIM_9X`, `AIM-9P5`). Types
  with the same key are counted together.
- A pylon *matches* a key when the key is in its weapon's pydcs name or CLSID,
  upper-cased, letters and digits only. A pylon that matches two keys gives
  the planned loadout (`{type} matches more than one weapon`, the first key's
  type in the tick's order): the tick's `AIM_9` (an AIM-9M) is in an AIM-9X's
  name too, so an aircraft with both gives its planned loadout.
- A pylon's *count* is read where the key stands in its pydcs name: a
  `<n> x`, `<n>x` or `<n>*` right before it (`MPRL - 8 x GBU-31(V)3/B` is 8,
  `BRU-57 with 2 x CBU-105 - 10 x SFW` is 2), else a `* <n>` right after it
  (`GBU-31(V)3/B * 2`, the F-15E's CFT pairs, is 2), else 1. Not the first
  such pattern in the name: `RBK-250 - 42 x PTAB-2.5M` is one bomb of 42
  bomblets, and the tick counts 1. A key found only in the CLSID takes the
  name's first `<n> x`, `<n>x` or `<n>*`, else 1.
- Not ammo, never emptied for ammo: a clean pylon (`<CLEAN>`), fuel tanks
  (pydcs name has `fuel tank`, any case), TGPs and jammers (`WeaponType.TGP`,
  `JAMMER`, `OFFENSIVE_JAMMER`), and a pylon that matches no key and whose
  name has `pod` or `targeting` (a nav pod; the F-16's AN/ASQ-213 HTS, `HARM
  Targeting System`, which the tick does not count and whose type is
  `unknown`). A rocket pod that matches a key is ammo.
- Any other pylon that matches no key carries a weapon the tick no longer
  counts: it is emptied. So is every such pylon when `ammo` is None or empty
  (the hook leaves `ammo` out when a unit carries none).
- Fewer of a type than planned: its pylons are emptied highest-numbered first
  until they hold no more than the tick counts (a rack partly fired is
  emptied whole: never more weapons than counted).
- The planned loadout, with the reason, when a type is on no pylon (`{type}
  is not on its pylons`) or more are counted than planned (`{count} {type}
  counted, {planned} planned`). Rockets usually land here: pydcs counts a
  rocket pod's rack (`2 x LAU-61 - 19 x UnGd Rkts, 70 mm Hydra 70 M151 HE` is
  1 by the rule above), the tick every rocket.
- Fuel: at 1 or below, every fuel tank pylon is emptied; over 1 or None, the
  tanks stay. This holds for the planned loadout too.

`resume_loadout` never changes the loadout it is given.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from game.ato.loadouts import Loadout
from game.data.weapons import Weapon, WeaponType

GUNS = "weapons.shells."
FUEL_TANK = "fuel tank"
KEPT_TYPES = {WeaponType.TGP, WeaponType.JAMMER, WeaponType.OFFENSIVE_JAMMER}
CLEAN = "<CLEAN>"
NOT_AMMO_WORDS = ("POD", "TARGETING")

_NOT_KEY = re.compile(r"[^A-Z0-9]")
_COUNT_BEFORE = re.compile(r"(\d+)\s*[x*]\s*$")
_COUNT_AFTER = re.compile(r"^\s*\*\s*(\d+)")
_COUNT_ANY = re.compile(r"(\d+)\s*[x*]")


@dataclass(frozen=True)
class ResumeLoadout:
    """The loadout to write; `reason` None when it is the tick's state, else
    why it is the planned loadout."""

    loadout: Loadout
    reason: Optional[str]


def resume_loadout(
    planned: Loadout,
    fuel: Optional[float],
    ammo: Optional[tuple[tuple[str, int], ...]],
) -> ResumeLoadout:
    """`planned` stripped down to the tick's `ammo` and `fuel`."""
    without_tanks = planned.clone()
    if fuel is not None and fuel <= 1.0:
        for n, weapon in planned.pylons.items():
            if weapon is not None and _is_tank(weapon):
                del without_tanks.pylons[n]
                without_tanks.pylon_settings.pop(n, None)

    types: dict[str, str] = {}  # key -> the tick's type, first seen
    counted: dict[str, int] = {}
    for dcs_type, count in ammo or ():
        if dcs_type.startswith(GUNS):
            continue
        key = _key(dcs_type.rsplit(".", 1)[-1])
        types.setdefault(key, dcs_type)
        counted[key] = counted.get(key, 0) + count

    on_pylons: dict[str, list[tuple[int, int]]] = {k: [] for k in types}
    emptied: list[int] = []
    for n, weapon in sorted(without_tanks.pylons.items()):
        if weapon is None or _never_ammo(weapon):
            continue
        hits = [k for k in types if k and _matches(weapon, k)]
        if len(hits) > 1:
            return ResumeLoadout(
                without_tanks, f"{types[hits[0]]} matches more than one weapon"
            )
        if hits:
            on_pylons[hits[0]].append((n, _count(weapon, hits[0])))
        elif not _pod(weapon):
            emptied.append(n)

    for key, pylons in on_pylons.items():
        if not pylons:
            return ResumeLoadout(without_tanks, f"{types[key]} is not on its pylons")
        total = sum(c for _, c in pylons)
        if counted[key] > total:
            return ResumeLoadout(
                without_tanks, f"{counted[key]} {types[key]} counted, {total} planned"
            )
        for n, c in reversed(pylons):
            if total <= counted[key]:
                break
            emptied.append(n)
            total -= c

    for n in emptied:
        del without_tanks.pylons[n]
        without_tanks.pylon_settings.pop(n, None)
    return ResumeLoadout(without_tanks, None)


def _key(text: str) -> str:
    return _NOT_KEY.sub("", text.upper())


def _matches(weapon: Weapon, key: str) -> bool:
    return key in _key(weapon.name) or key in _key(weapon.clsid)


def _is_tank(weapon: Weapon) -> bool:
    return FUEL_TANK in weapon.name.lower()


def _never_ammo(weapon: Weapon) -> bool:
    return (
        weapon.clsid == CLEAN
        or _is_tank(weapon)
        or weapon.weapon_group.type in KEPT_TYPES
    )


def _pod(weapon: Weapon) -> bool:
    name = weapon.name.upper()
    return any(word in name for word in NOT_AMMO_WORDS)


def _count(weapon: Weapon, key: str) -> int:
    """How many of the weapon `key` names one pylon carries."""
    name = weapon.name
    at = [i for i, c in enumerate(name) if _NOT_KEY.sub("", c.upper())]
    start = "".join(name[i].upper() for i in at).find(key)
    if start < 0:
        m = _COUNT_ANY.search(name)
        return int(m.group(1)) if m else 1
    before = _COUNT_BEFORE.search(name[: at[start]])
    if before:
        return int(before.group(1))
    after = _COUNT_AFTER.match(name[at[start + len(key) - 1] + 1 :])
    return int(after.group(1)) if after else 1
