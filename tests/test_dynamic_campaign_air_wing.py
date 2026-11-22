"""A dynamic campaign's squadrons at turn 0 and across turn initializations.

A dynamic campaign keeps its ATO when a turn is initialized: the flights
planned by hand or launched hold their aircraft and pilots, the rest of each
squadron is free to task. It always starts with its squadrons full.

The games are generated headless from a campaign shipped with Retribution
(`new_game`, in conftest), created as the new game wizard does: the flag
first, then `begin_turn_0`.
"""

from __future__ import annotations

from pathlib import Path

from game import persistency
from game.ato.flight import Flight
from game.ato.flightroster import FlightRoster
from game.ato.flighttype import FlightType
from game.ato.package import Package
from game.ato.starttype import StartType
from game.migrator import Migrator
from game.sim import GameUpdateEvents
from game.theater import Airfield, ParkingType
from tests.conftest import generate_starting_mission, new_game, plan_by_hand, squadrons


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


def test_dynamic_campaign_starting_mission_keeps_pilots_free(
    saved_games: Path, tmp_path: Path
) -> None:
    """The starting mission parks the untasked aircraft; they hold no pilot."""
    game = new_game(dynamic=True, squadrons_start_full=True)
    flight = plan_by_hand(game, 2)
    pilots = [p for p in flight.roster.iter_pilots() if p is not None]
    game.pass_turn(no_action=True)

    generate_starting_mission(game, tmp_path)

    for s in squadrons(game):
        free = [p for p in s.active_pilots if not any(p is q for q in pilots)]
        assert s.available_pilots == free, s
    # A flight created as the Create flight dialog does, as big as the airfield
    # squadron that parked the most aircraft can crew, has all its pilots.
    squadron = max(
        (
            s
            for s in game.blue.air_wing.iter_squadrons()
            if isinstance(s.location, Airfield)
        ),
        key=lambda s: s.untasked_aircraft,
    )
    size = min(squadron.untasked_aircraft, len(squadron.active_pilots))
    package = Package(squadron.location, game.db.flights, auto_asap=True)
    roster = FlightRoster(squadron, initial_size=size)
    created = Flight(
        package, squadron, size, FlightType.BARCAP, StartType.COLD, None, roster=roster
    )
    assert created.missing_pilots == 0


def test_normal_campaign_unchanged(saved_games: Path, tmp_path: Path) -> None:
    """Not full when not ticked; Begin Campaign clears the hand-planned flight."""
    assert all(
        s.owned_aircraft == 0
        for s in squadrons(new_game(dynamic=False, squadrons_start_full=False))
    )

    game = new_game(dynamic=False, squadrons_start_full=True)
    flight = plan_by_hand(game, 2)
    game.pass_turn(no_action=True)

    assert flight not in [f for p in game.blue.ato.packages for f in p.flights]

    # The parked aircraft hold a pilot each until the next turn, as before.
    held = sum(
        f.count
        for c in (game.blue, game.red)
        for p in c.ato.packages
        for f in p.flights
    )
    generate_starting_mission(game, tmp_path)
    active = sum(len(s.active_pilots) for s in squadrons(game))
    available = sum(len(s.available_pilots) for s in squadrons(game))
    assert available < active - held
