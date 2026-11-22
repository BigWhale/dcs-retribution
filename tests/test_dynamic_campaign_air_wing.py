"""A dynamic campaign's squadrons at turn 0 and across turn initializations.

A dynamic campaign keeps its ATO when a turn is initialized: the flights
planned by hand or launched hold their aircraft and pilots, the rest of each
squadron is free to task. It always starts with its squadrons full.

The games are generated headless from a campaign shipped with Retribution,
created as the new game wizard does: the flag first, then `begin_turn_0`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from game import persistency
from game.ato.flight import Flight
from game.ato.flighttype import FlightType
from game.ato.package import Package
from game.ato.starttype import StartType
from game.campaignloader.campaign import Campaign, DEFAULT_BUDGET
from game.factions import FACTIONS
from game.game import Game
from game.migrator import Migrator
from game.settings import Settings
from game.sim import GameUpdateEvents
from game.squadrons import Squadron
from game.theater import ParkingType
from game.theater.start_generator import GameGenerator, GeneratorSettings, ModSettings
from pydcs_extensions import load_mods

CAMPAIGN = Path("resources/campaigns/battle_of_abu_dhabi.yaml")


@pytest.fixture(scope="module")
def saved_games(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from qt_ui.main import inject_custom_payloads

    folder = tmp_path_factory.mktemp("saved_games")
    persistency.setup(str(folder), False, 16880)
    load_mods()
    inject_custom_payloads(folder)
    return folder


def new_game(dynamic: bool, squadrons_start_full: bool) -> Game:
    campaign = Campaign.from_file(CAMPAIGN)
    theater = campaign.load_theater(False)
    game = GameGenerator(
        FACTIONS[campaign.recommended_player_faction],
        FACTIONS[campaign.recommended_enemy_faction],
        theater,
        campaign.load_air_wing_config(theater),
        Settings(),
        GeneratorSettings(
            start_date=datetime(2015, 6, 1),
            start_time=campaign.recommended_start_time,
            player_budget=DEFAULT_BUDGET,
            enemy_budget=DEFAULT_BUDGET,
            inverted=False,
            advanced_iads=theater.iads_network.advanced_iads,
            no_carrier=False,
            no_lha=False,
            no_player_navy=False,
            no_enemy_navy=False,
            squadrons_start_full=squadrons_start_full,
            tgo_config=campaign.load_ground_forces_config(),
            carrier_config=campaign.load_carrier_config(),
        ),
        ModSettings(**{f: False for f in ModSettings.__dataclass_fields__}),
    ).generate()
    game.dynamic_campaign = dynamic
    game.begin_turn_0(squadrons_start_full=squadrons_start_full)
    return game


def squadrons(game: Game) -> list[Squadron]:
    return [s for c in (game.blue, game.red) for s in c.air_wing.iter_squadrons()]


def plan_by_hand(game: Game, count: int, squadron: Squadron | None = None) -> Flight:
    if squadron is None:
        squadron = next(
            s
            for s in game.blue.air_wing.iter_squadrons()
            if s.untasked_aircraft >= 2 * count
        )
    package = Package(squadron.location, game.db.flights, auto_asap=True)
    flight = Flight(package, squadron, count, FlightType.BARCAP, StartType.COLD, None)
    package.add_flight(flight)
    game.blue.ato.add_package(package)
    return flight


def test_dynamic_campaign_starts_full_and_ready(saved_games: Path) -> None:
    """Squadrons start full whatever the wizard's checkbox says, all free."""
    game = new_game(dynamic=True, squadrons_start_full=False)

    assert sum(s.owned_aircraft for s in squadrons(game)) > 0
    for s in squadrons(game):
        parking = s.location.unclaimed_parking(ParkingType().from_squadron(s))
        assert s.owned_aircraft == s.max_size or parking == 0, s
        assert s.untasked_aircraft == s.owned_aircraft, s
        assert s.available_pilots == s.active_pilots, s


def test_dynamic_campaign_keeps_held_aircraft_and_pilots(saved_games: Path) -> None:
    """A flight planned at turn 0 is kept by Begin Campaign and holds its own."""
    game = new_game(dynamic=True, squadrons_start_full=True)
    flight = plan_by_hand(game, 2)
    pilots = [p for p in flight.roster.iter_pilots() if p is not None]
    assert len(pilots) == 2

    game.pass_turn(no_action=True)  # as Begin Campaign does
    game.initialize_turn(GameUpdateEvents())  # a mid-turn re-initialization

    assert flight in [f for p in game.blue.ato.packages for f in p.flights]
    for s in squadrons(game):
        held = flight.count if s is flight.squadron else 0
        assert s.untasked_aircraft == s.owned_aircraft - held, s
        free = [p for p in s.active_pilots if not any(p is q for q in pilots)]
        assert s.available_pilots == free, s


def test_dynamic_campaign_loaded_keeps_held_aircraft_and_pilots(
    saved_games: Path,
) -> None:
    """A save loaded as Retribution loads it: the flight holds its own.

    The flight is the squadron's second; the first one is deleted, so its
    pilots are not the ones the squadron would hand out next.
    """
    game = new_game(dynamic=True, squadrons_start_full=True)
    first = plan_by_hand(game, 2)
    flight = plan_by_hand(game, 2, first.squadron)
    first.package.remove_flight(first)
    game.blue.ato.remove_package(first.package)
    game.pass_turn(no_action=True)  # autosaves

    loaded = persistency.load_game(str(persistency.save_dir() / "autosave.retribution"))
    assert loaded is not None
    Migrator(loaded, is_liberation=False)

    [flight] = [
        f for p in loaded.blue.ato.packages for f in p.flights if f.id == flight.id
    ]
    pilots = [p for p in flight.roster.iter_pilots() if p is not None]
    for s in squadrons(loaded):
        held = flight.count if s is flight.squadron else 0
        assert s.untasked_aircraft == s.owned_aircraft - held, s
        free = [p for p in s.active_pilots if not any(p is q for q in pilots)]
        assert s.available_pilots == free, s


def test_normal_campaign_unchanged(saved_games: Path) -> None:
    """Not full when not ticked; Begin Campaign clears the hand-planned flight."""
    assert all(
        s.owned_aircraft == 0
        for s in squadrons(new_game(dynamic=False, squadrons_start_full=False))
    )

    game = new_game(dynamic=False, squadrons_start_full=True)
    flight = plan_by_hand(game, 2)
    game.pass_turn(no_action=True)

    assert flight not in [f for p in game.blue.ato.packages for f in p.flights]
