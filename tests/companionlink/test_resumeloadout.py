"""A resumed aircraft's loadout, from the tick's `ammo` and `fuel`.

Against real data: `data/resume_ammo.json` holds airborne AI aircraft of
lake's saves `ammo_capture.retribution` (tick at minute 4) and
`test_dynamic_pg.retribution` (minute 20), each its planned pylons (pylon
number to CLSID), the tick's `ammo` as the hook wrote it, and `full`, true
when the aircraft had not fired yet.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from game.ato.loadouts import Loadout
from game.companionlink.resumeloadout import resume_loadout
from game.data.weapons import Weapon, WeaponType

FIXTURE: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "data" / "resume_ammo.json").read_text()
)
FULL = [e for e in FIXTURE if e["full"]]

#: The fixture's weapons as the tick counts them, read off the capture by
#: hand: CLSID -> (the tick's weapon type, how many one pylon carries). The
#: GBU-31(V)2/B was dropped before its tick, so its type was not seen. A CLSID
#: not here (tanks, TGPs, jammers, the HTS, the nav pod, a clean pylon)
#: carries no ammo.
RACKS: dict[str, tuple[Optional[str], int]] = {
    "{5CE2FF2A-645A-4197-B48D-8720AC69394F}": ("AIM_9X", 1),
    "{6CEB49FC-DED8-4DED-B053-E1F033FF72D3}": ("AIM_9", 1),
    "{AIM-9P5}": ("AIM-9P5", 1),
    "{40EF17B7-F508-45de-8566-6FFECC0C1AB8}": ("weapons.missiles.AIM_120C", 1),
    "LAU_117_AGM_65F": ("weapons.missiles.AGM_65F", 1),
    "LAU_117_AGM_65G": ("weapons.missiles.AGM_65G", 1),
    "{B06DD79A-F21E-4EB9-BD9D-AB3844618C93}": ("weapons.missiles.AGM_88", 1),
    "GBU-31V3B*8": ("weapons.bombs.GBU_31_V_3B", 8),
    "{GBU-31V3B}": ("weapons.bombs.GBU_31_V_3B", 1),
    "{CFT_L_GBU_31V3B_x_2}": ("weapons.bombs.GBU_31_V_3B", 2),
    "{CFT_R_GBU_31V3B_x_2}": ("weapons.bombs.GBU_31_V_3B", 2),
    "{BRU57_2*CBU-105}": ("weapons.bombs.CBU_105", 2),
    "{4203753F-8198-4E85-9924-6F8FF679F9FF}": ("weapons.bombs.RBK_250", 1),
    "{GBU_31_V_2B}": (None, 1),
}

KEPT_TYPES = {WeaponType.TGP, WeaponType.JAMMER, WeaponType.OFFENSIVE_JAMMER}

Ammo = tuple[tuple[str, int], ...]


def planned(entry: dict[str, Any]) -> Loadout:
    pylons: dict[int, Optional[Weapon]] = {}
    for n, clsid in entry["pylons"].items():
        weapon = Weapon.with_clsid(clsid)
        assert weapon is not None, clsid
        pylons[int(n)] = weapon
    return Loadout("planned", pylons, None)


def ammo(entry: dict[str, Any]) -> Ammo:
    return tuple((t, c) for t, c in entry["ammo"])


def weapon_types(entry: dict[str, Any]) -> list[tuple[str, int]]:
    """The tick's weapons of an entry, guns left out."""
    return [(t, c) for t, c in entry["ammo"] if not t.startswith("weapons.shells.")]


def clsids(loadout: Loadout) -> dict[int, str]:
    return {n: w.clsid for n, w in loadout.pylons.items() if w is not None}


def of_type(entry: dict[str, Any], dcs_type: str) -> list[int]:
    """The entry's pylons that carry `dcs_type`, lowest number first."""
    return sorted(
        int(n)
        for n, clsid in entry["pylons"].items()
        if clsid in RACKS and RACKS[clsid][0] == dcs_type
    )


def without(entry: dict[str, Any], pylons: list[int]) -> dict[int, str]:
    return {
        int(n): clsid
        for n, clsid in entry["pylons"].items()
        if int(n) not in set(pylons)
    }


def gone(entry: dict[str, Any]) -> list[int]:
    """The pylons of weapons the entry's tick no longer counts."""
    counted = {t for t, _ in entry["ammo"]}
    return [
        int(n)
        for n, clsid in entry["pylons"].items()
        if clsid in RACKS and RACKS[clsid][0] not in counted
    ]


def label(entry: dict[str, Any]) -> str:
    return f"{entry['aircraft']} {sorted(set(entry['pylons'].values()))}"


def test_fixture_has_the_data_the_rules_rest_on() -> None:
    assert len(FIXTURE) >= 6
    assert len({e["aircraft"] for e in FIXTURE}) >= 4
    assert any(not e["full"] for e in FIXTURE)


@pytest.mark.parametrize("entry", FULL, ids=label)
def test_full_load_keeps_every_pylon(entry: dict[str, Any]) -> None:
    plan = planned(entry)
    before = clsids(plan)
    resumed = resume_loadout(plan, None, ammo(entry))
    assert resumed.reason is None
    assert clsids(resumed.loadout) == before
    assert clsids(plan) == before


def test_fired_aircraft_from_the_tick() -> None:
    """The fixture's aircraft that had dropped its bombs at its tick."""
    for entry in [e for e in FIXTURE if not e["full"]]:
        resumed = resume_loadout(planned(entry), 1.1, ammo(entry))
        assert resumed.reason is None
        assert gone(entry)
        assert clsids(resumed.loadout) == without(entry, gone(entry))


@pytest.mark.parametrize("entry", FIXTURE, ids=label)
def test_fired_pylons_emptied(entry: dict[str, Any]) -> None:
    for dcs_type, count in weapon_types(entry):
        pylons = of_type(entry, dcs_type)
        assert pylons, dcs_type
        highest = pylons[-1]
        rack = RACKS[entry["pylons"][str(highest)]][1]
        for fired in sorted({rack, 1}):
            left = tuple((t, c - fired if t == dcs_type else c) for t, c in ammo(entry))
            plan = planned(entry)
            before = clsids(plan)
            resumed = resume_loadout(plan, None, left)
            assert resumed.reason is None, (dcs_type, fired)
            assert clsids(resumed.loadout) == without(entry, [highest] + gone(entry)), (
                dcs_type,
                fired,
            )
            assert clsids(plan) == before


def test_fired_racks_empty_until_no_more_than_counted() -> None:
    """The F-15E's six GBU-31(V)3/B: two singles, two CFT pairs (pylons 4,
    12). Three left empties 14 and 12; a count lowered by less than a rack's
    worth empties that rack too."""
    entry = next(e for e in FIXTURE if e["aircraft"].startswith("F-15E"))
    bombs = "weapons.bombs.GBU_31_V_3B"
    for left, emptied in [(5, [14]), (4, [12, 14]), (3, [12, 14]), (2, [4, 12, 14])]:
        counted = tuple((t, left if t == bombs else c) for t, c in ammo(entry))
        resumed = resume_loadout(planned(entry), None, counted)
        assert resumed.reason is None
        assert clsids(resumed.loadout) == without(entry, emptied), left
    b1 = next(e for e in FIXTURE if e["aircraft"].startswith("B-1B"))
    resumed = resume_loadout(planned(b1), None, ((bombs, 23),))
    assert clsids(resumed.loadout) == without(b1, [3])


@pytest.mark.parametrize("entry", FULL, ids=label)
def test_type_gone_empties_its_pylons(entry: dict[str, Any]) -> None:
    for dcs_type, _ in weapon_types(entry):
        left = tuple((t, c) for t, c in ammo(entry) if t != dcs_type)
        resumed = resume_loadout(planned(entry), None, left)
        assert resumed.reason is None
        assert clsids(resumed.loadout) == without(entry, of_type(entry, dcs_type))
        for n, w in planned(entry).pylons.items():
            if w is not None and w.weapon_group.type in KEPT_TYPES:
                assert resumed.loadout.pylons.get(n) is w


def test_type_gone_keeps_tgps_and_jammers() -> None:
    kept = [
        e
        for e in FULL
        if any(
            Weapon.with_clsid(c).weapon_group.type in KEPT_TYPES  # type: ignore[union-attr]
            for c in e["pylons"].values()
        )
    ]
    assert len(kept) >= 3


@pytest.mark.parametrize("entry", FULL, ids=label)
def test_unmatched_type_gives_planned(entry: dict[str, Any]) -> None:
    plan = planned(entry)
    resumed = resume_loadout(
        plan, None, ammo(entry) + (("weapons.missiles.NOPE_9", 1),)
    )
    assert resumed.reason == "weapons.missiles.NOPE_9 is not on its pylons"
    assert clsids(resumed.loadout) == clsids(plan)

    dcs_type, count = weapon_types(entry)[0]
    more = tuple((t, c + 1 if t == dcs_type else c) for t, c in ammo(entry))
    resumed = resume_loadout(plan, None, more)
    assert resumed.reason == f"{count + 1} {dcs_type} counted, {count} planned"
    assert clsids(resumed.loadout) == clsids(plan)


def test_pylon_matching_two_types_gives_planned() -> None:
    """AIM_9 (an AIM-9M) is in the AIM-9X's name too."""
    entry = next(e for e in FULL if "AIM_9X" in dict(e["ammo"]))
    plan = planned(entry)
    resumed = resume_loadout(plan, None, ammo(entry) + (("AIM_9", 1),))
    assert resumed.reason == "AIM_9X matches more than one weapon"
    assert clsids(resumed.loadout) == clsids(plan)


@pytest.mark.parametrize("entry", FIXTURE, ids=label)
@pytest.mark.parametrize("none_left", [None, ()], ids=["no field", "empty"])
def test_no_ammo_field_means_none_left(
    entry: dict[str, Any], none_left: Optional[Ammo]
) -> None:
    resumed = resume_loadout(planned(entry), 1.5, none_left)
    weapons = [int(n) for n, c in entry["pylons"].items() if c in RACKS]
    assert weapons
    assert resumed.reason is None
    assert clsids(resumed.loadout) == without(entry, weapons)


def test_no_ammo_keeps_tgps_jammers_and_pods() -> None:
    kept = set()
    for entry in FIXTURE:
        resumed = resume_loadout(planned(entry), 1.5, None)
        kept |= {w.name for w in resumed.loadout.pylons.values() if w is not None}
    assert "AN/AAQ-33 - Advanced Targeting Pod" in kept
    assert "ALQ-184 - ECM Pod" in kept
    assert "MPS-410" in kept
    assert "AN/AAQ-13 LANTIRN NAV POD" in kept
    assert "AN/ASQ-213 HTS - HARM Targeting System" in kept


@pytest.mark.parametrize("entry", FIXTURE, ids=label)
def test_guns_ignored(entry: dict[str, Any]) -> None:
    counted = tuple(
        (t, c) for t, c in ammo(entry) if not t.startswith("weapons.shells.")
    )
    plain = resume_loadout(planned(entry), None, counted)
    guns = resume_loadout(
        planned(entry), None, counted + (("weapons.shells.M61_20_HE", 500),)
    )
    assert guns.reason == plain.reason
    assert clsids(guns.loadout) == clsids(plain.loadout)


def test_fuel_tanks() -> None:
    tanked = [
        e
        for e in FULL
        if any(
            "fuel tank" in Weapon.with_clsid(c).name.lower()  # type: ignore[union-attr]
            for c in e["pylons"].values()
        )
    ]
    assert len(tanked) >= 4
    for entry in tanked:
        tanks = [
            int(n)
            for n, c in entry["pylons"].items()
            if "fuel tank" in Weapon.with_clsid(c).name.lower()  # type: ignore[union-attr]
        ]
        for fuel in (1.2, None):
            resumed = resume_loadout(planned(entry), fuel, ammo(entry))
            assert resumed.reason is None
            assert clsids(resumed.loadout) == without(entry, []), fuel
        for fuel in (0.8, 1.0):
            resumed = resume_loadout(planned(entry), fuel, ammo(entry))
            assert resumed.reason is None
            assert clsids(resumed.loadout) == without(entry, tanks), fuel
