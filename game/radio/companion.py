"""The frequencies DCS Companion's stations use in a mission.

DCS Companion (https://github.com/BigWhale/dcs-companion) answers radio calls
over SRS. With the "Using DCS Companion & SRS" setting on, Retribution plans
the frequencies of its ATIS, White Force and AWACS stations while it generates
the mission, so they can go in the player aircraft's presets, on the kneeboard
and into the mission for the app. The rules are the app's own planners'.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Generic, Hashable, Iterable, Optional, Sequence, TypeVar

from dcs.task import Modulation

from game.radio.radios import COMPANION_CLEARANCE_HZ, Radio, RadioFrequency

#: Frequencies are 25 kHz apart; one within 25 kHz of a busy one is not free.
STEP_HZ = COMPANION_CLEARANCE_HZ

VHF_LO = 125_000_000
VHF_HI = 136_975_000
UHF_LO = 260_000_000
UHF_HI = 289_975_000
#: The AWACS UHF stays below the F-4E aux radio's 284.900.
AWACS_UHF_HI = 284_875_000

KeyT = TypeVar("KeyT", bound=Hashable)


@dataclass(frozen=True)
class CompanionPair:
    """A station's VHF and UHF, AM; a band left out by a full range is None."""

    vhf: Optional[RadioFrequency]
    uhf: Optional[RadioFrequency]

    def frequencies(self) -> list[RadioFrequency]:
        return [f for f in (self.vhf, self.uhf) if f is not None]

    def on_radio(self, radio: Optional[Radio]) -> Optional[RadioFrequency]:
        """The one to put on a radio: UHF when it tunes it, else VHF.

        A radio not known is taken for UHF. None when it tunes neither.
        """
        for frequency in (self.uhf, self.vhf):
            if frequency is not None and (radio is None or tunes(radio, frequency)):
                return frequency
        return None


def tunes(radio: Radio, frequency: RadioFrequency) -> bool:
    """True when one of the radio's AM ranges holds the frequency."""
    return any(
        r.modulation is Modulation.AM
        and r.minimum.hertz <= frequency.hertz < r.maximum.hertz
        for r in radio.ranges
    )


@dataclass
class CompanionPlan(Generic[KeyT]):
    """The companion's frequencies for one mission."""

    #: ATIS pairs by DCS airbase id.
    atis: dict[int, CompanionPair]
    #: White Force's UHF, None when the range is full.
    white_force: Optional[RadioFrequency]
    #: AWACS pairs by AEWC flight, in ATO order.
    awacs: dict[KeyT, CompanionPair]
    #: The DCS group name of each AWACS flight, once its group is generated.
    awacs_groups: dict[KeyT, str] = field(default_factory=dict)

    def frequencies(self) -> list[RadioFrequency]:
        """Every frequency of the plan."""
        picked = [f for pair in self.atis.values() for f in pair.frequencies()]
        if self.white_force is not None:
            picked.append(self.white_force)
        picked.extend(f for pair in self.awacs.values() for f in pair.frequencies())
        return picked

    def name_awacs(self, key: KeyT, group_name: str) -> None:
        """Records the DCS group name of a planned AWACS flight."""
        if key in self.awacs:
            self.awacs_groups[key] = group_name

    def awacs_by_group(self) -> dict[str, CompanionPair]:
        """The AWACS pairs of the flights whose group was generated."""
        return {
            name: self.awacs[key]
            for key, name in self.awacs_groups.items()
            if key in self.awacs
        }

    def lua(self, subtitle_file: Optional[str] = None) -> str:
        """The plan as `dcsRetribution.Companion`, for DCS Companion's hook.

        Version 1: ATIS pairs by DCS airbase id, White Force, AWACS pairs by DCS
        group name; MHz as numbers, AM implied; a band left out is absent.
        With a subtitle file (the mission's path of the silent sound), `subtitle`
        names it, and the app shows its replies as DCS radio subtitles.
        """
        atis = "".join(
            f"    [{airbase}] = {_lua_pair(pair)},\n"
            for airbase, pair in sorted(self.atis.items())
        )
        awacs = "".join(
            f"    [{_lua_string(name)}] = {_lua_pair(pair)},\n"
            for name, pair in self.awacs_by_group().items()
        )
        white_force = _lua_pair(CompanionPair(vhf=None, uhf=self.white_force))
        subtitle = (
            f"  subtitle = {{ file = {_lua_string(subtitle_file)} }},\n"
            if subtitle_file is not None
            else ""
        )
        return (
            "dcsRetribution.Companion = {\n"
            "  version = 1,\n"
            f"  ATIS = {{\n{atis}  }},\n"
            f"  WhiteForce = {white_force},\n"
            f"  AWACS = {{\n{awacs}  }},\n"
            f"{subtitle}"
            "}\n"
        )


def _lua_pair(pair: CompanionPair) -> str:
    bands = [
        f"{band} = {f.hertz / 1_000_000:.3f}"
        for band, f in (("vhf", pair.vhf), ("uhf", pair.uhf))
        if f is not None
    ]
    return "{" + ", ".join(bands) + "}"


def _lua_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def plan_companion(
    busy: Iterable[RadioFrequency],
    airfields: Iterable[int],
    awacs: Sequence[KeyT],
) -> CompanionPlan[KeyT]:
    """Plans the companion's frequencies clear of the busy ones.

    Args:
        busy: Every frequency already in use in the mission, any modulation.
        airfields: The DCS airbase ids of the blue airfields.
        awacs: The blue AEWC flights, in ATO order.

    ATIS first, each airfield by airbase id the lowest free VHF and UHF; then
    White Force, the highest free UHF; then each AWACS the highest free VHF and
    the highest free UHF below 284.900. Free is at least 25 kHz from every busy
    frequency and from everything picked before.
    """
    taken = [f.hertz for f in busy]

    def pick(hertzes: range, what: str) -> Optional[RadioFrequency]:
        for hz in hertzes:
            if all(abs(hz - t) >= STEP_HZ for t in taken):
                taken.append(hz)
                return RadioFrequency(hz)
        logging.warning(f"DCS Companion: no free frequency for {what}")
        return None

    def up(lo: int, hi: int) -> range:
        return range(lo, hi + 1, STEP_HZ)

    def down(lo: int, hi: int) -> range:
        return range(hi, lo - 1, -STEP_HZ)

    atis: dict[int, CompanionPair] = {}
    for airbase in sorted(set(airfields)):
        pair = CompanionPair(
            vhf=pick(up(VHF_LO, VHF_HI), f"the ATIS VHF of airbase {airbase}"),
            uhf=pick(up(UHF_LO, UHF_HI), f"the ATIS UHF of airbase {airbase}"),
        )
        if pair.frequencies():
            atis[airbase] = pair

    white_force = pick(down(UHF_LO, UHF_HI), "White Force")

    awacs_pairs: dict[KeyT, CompanionPair] = {}
    for key in awacs:
        pair = CompanionPair(
            vhf=pick(down(VHF_LO, VHF_HI), f"the AWACS VHF of {key}"),
            uhf=pick(down(UHF_LO, AWACS_UHF_HI), f"the AWACS UHF of {key}"),
        )
        if pair.frequencies():
            awacs_pairs[key] = pair

    return CompanionPlan(atis=atis, white_force=white_force, awacs=awacs_pairs)
