"""A dynamic campaign's restored mission: the newest snapshot of the campaign's
own mission (the tick) kept in the save.

The link hands each snapshot over with its envelope compressed; the live
record keeps the newest one whose mission is the campaign's own under the
name map's marker, so the tick and the name map always belong to the same
mission. The tick is saved with the game; a save from before it loads with
none.

The games are generated headless from a campaign shipped with Retribution,
created as the new game wizard does: the flag first, then `begin_turn_0`.
"""

from __future__ import annotations

import json
import pickle
import zlib
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, cast

import pytest

from game import persistency
from game.companionlink.liverecord import MARKER_PREFIX, MissionVerdict
from game.companionlink.liveunitmap import LiveUnitMap
from game.companionlink.snapshot import LiveSnapshot, parse_snapshot
from game.game import Game
from game.migrator import Migrator
from tests.conftest import generate_starting_mission, new_game

CAMPAIGN = Path("resources/campaigns/golan_heights_lite.yaml")
#: When the snapshots' missions started.
START = datetime(2015, 6, 1, 8, 0)


@pytest.fixture(scope="module")
def begun(saved_games: Path, tmp_path_factory: pytest.TempPathFactory) -> Game:
    """Begin Campaign's game: its mission written with a marker and name map."""
    game = new_game(dynamic=True, squadrons_start_full=True, campaign_file=CAMPAIGN)
    game.pass_turn(no_action=True)
    game.live.new_marker()
    sim = generate_starting_mission(game, tmp_path_factory.mktemp("mission"))
    assert sim.unit_map is not None
    game.live.written(LiveUnitMap.from_generation(sim.unit_map, game))
    return game


@pytest.fixture
def game(begun: Game) -> Game:
    """A copy of Begin Campaign's game, for a test that changes it."""
    return cast(Game, pickle.loads(pickle.dumps(begun)))


def envelope(
    game: Game,
    boot: str,
    minute: int,
    own: bool = True,
    timed: bool = True,
    groups: Optional[list[dict[str, Any]]] = None,
) -> str:
    """The companion's snapshot of the mission `boot` (session 1) at its
    `minute`, the mission started at START; the campaign's own carries the
    marker written last, an untimed one has no date or absTime."""
    statics = [{"name": MARKER_PREFIX + str(game.live.marker), "id": 9000}]
    at = START + timedelta(minutes=minute)
    midnight = datetime(at.year, at.month, at.day)
    meta: dict[str, Any] = {"modelTime": 60.0 * minute}
    if timed:
        meta["date"] = {"Year": at.year, "Month": at.month, "Day": at.day}
        meta["absTime"] = (at - midnight).total_seconds()
    return json.dumps(
        {
            "minute": minute,
            "boot": boot,
            "session": 1,
            "modelTime": 60.0 * minute,
            "state": {
                "meta": meta,
                "groups": groups or [],
                "statics": statics if own else [],
                "airbases": [],
            },
        }
    )


def tank(name: str, x: float, y: float) -> dict[str, Any]:
    """A ground group of one tank, `name` and its unit `name-1`."""
    return {
        "name": name,
        "cat": 2,
        "id": 100,
        "side": 1,
        "units": [
            {"name": f"{name}-1", "id": 101, "type": "T-55", "x": x, "y": y, "hdg": 90}
        ],
    }


def taken(data: str) -> LiveSnapshot:
    """The snapshot as the link hands it over: parsed, its envelope packed."""
    return replace(parse_snapshot(data), packed=zlib.compress(data.encode(), 6))


def save_and_load(game: Game, path: Path) -> Game:
    game.savepath = str(path)
    assert persistency.save_game(game)
    loaded = persistency.load_game(game.savepath)
    assert loaded is not None
    Migrator(loaded, is_liberation=False)
    return loaded


def test_tick_kept_only_for_the_name_maps_mission(game: Game) -> None:
    record = game.live
    own = taken(envelope(game, "b1", 3, groups=[tank("Armor", 1000.0, -2000.0)]))
    assert record.decide(own) is MissionVerdict.OWN
    assert record.keep(own)
    tick = record.tick
    assert tick is not None
    assert (tick.boot, tick.session, tick.minute) == ("b1", 1, 3)
    assert tick.model_time == 180.0
    assert tick.abs_time == START + timedelta(minutes=3)
    restored = tick.snapshot()
    assert restored.names == own.names
    assert restored.units == own.units

    foreign = taken(envelope(game, "b2", 4, own=False))
    assert record.decide(foreign) is MissionVerdict.FOREIGN
    assert not record.keep(foreign)
    assert record.tick is tick

    untimed = taken(envelope(game, "b1", 5, timed=False))
    assert record.decide(untimed) is MissionVerdict.OWN
    assert not record.keep(untimed), "a restore needs the minute"
    assert record.tick is tick

    unpacked = parse_snapshot(envelope(game, "b1", 5))
    assert not record.keep(unpacked)
    assert record.tick is tick

    again = taken(envelope(game, "b3", 6))
    assert record.decide(again) is MissionVerdict.OWN_AGAIN
    assert record.keep(again)
    assert record.tick is not None
    assert (record.tick.boot, record.tick.minute) == ("b3", 6)

    # A newer mission written and loaded: the name map is its, and a snapshot
    # of the earlier mission, still running, is not kept.
    record.new_marker()
    record.written(LiveUnitMap())
    newer = taken(envelope(game, "b4", 1))
    assert record.decide(newer) is MissionVerdict.OWN
    assert record.keep(newer)
    late = taken(envelope(game, "b3", 7, own=False))  # its marker is the earlier
    assert record.decide(late) is MissionVerdict.OWN_AGAIN
    assert not record.keep(late)
    assert record.tick is not None and record.tick.boot == "b4"


def test_tick_survives_save_and_load(game: Game, tmp_path: Path) -> None:
    own = taken(envelope(game, "b1", 12, groups=[tank("Armor", 1000.0, -2000.0)]))
    game.live.decide(own)
    assert game.live.keep(own)
    tick = game.live.tick
    assert tick is not None

    loaded = save_and_load(game, tmp_path / "tick.retribution").live.tick
    assert loaded is not None
    assert loaded.packed == tick.packed
    assert loaded.minute == 12
    assert loaded.abs_time == START + timedelta(minutes=12)
    assert loaded.snapshot().names == own.names


def test_slice_one_save_loads_without_tick(game: Game, tmp_path: Path) -> None:
    del game.live.__dict__["tick"]
    loaded = save_and_load(game, tmp_path / "slice1.retribution")

    assert loaded.live.tick is None
    record = loaded.live
    own = taken(envelope(loaded, "b1", 2))
    assert record.decide(own) is MissionVerdict.OWN
    assert record.keep(own)
    assert record.tick is not None and record.tick.minute == 2
