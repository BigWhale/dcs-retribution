"""What the tests share: the fakes of the companion's two ports, the saved
games folder, and games generated headless as the new game wizard does.

The link fake (`FakeCompanion`) serves `GET /v1/hello`, `GET /v1/stream` and
`GET /v1/events` from a real HTTP server on 127.0.0.1, one script per stream
connection, with the field names of the companion's minute snapshots and of
its event log. The command fake (`FakeCommands`) answers `POST /v1/spawn`,
`GET /v1/spawns`, `GET /v1/spawns/{id}` and `POST /v1/despawn` as the
companion does (`docs/reference/campaign.md`, Command port).

A game is generated from a campaign shipped with Retribution, created as the
new game wizard does: the flag first, then `begin_turn_0`.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Union
from urllib.parse import parse_qs, urlsplit

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
from game.sim.missionsimulation import MissionSimulation
from game.squadrons import Squadron
from game.theater.start_generator import GameGenerator, GeneratorSettings, ModSettings
from pydcs_extensions import load_mods

# Games.

#: The campaign `new_game` generates when given none.
CAMPAIGN = Path("resources/campaigns/battle_of_abu_dhabi.yaml")


@pytest.fixture(scope="module")
def saved_games(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from qt_ui.main import inject_custom_payloads

    folder = tmp_path_factory.mktemp("saved_games")
    # persistency creates missing folders with mode 755 *decimal*, which leaves
    # them unlistable; mission generation lists the kneeboards folder.
    (folder / "Retribution" / "Kneeboards").mkdir(parents=True)
    persistency.setup(str(folder), False, 16880)
    load_mods()
    inject_custom_payloads(folder)
    return folder


def new_game(
    dynamic: bool, squadrons_start_full: bool, campaign_file: Path = CAMPAIGN
) -> Game:
    campaign = Campaign.from_file(campaign_file)
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
    flight.recreate_flight_plan()
    package.set_tot_asap(game.conditions.start_time)
    squadron.coalition.ato.add_package(package)
    return flight


def save_and_load(game: Game, path: Path) -> Game:
    """Saves the game to `path` and loads it again, as opening a save does."""
    game.savepath = str(path)
    assert persistency.save_game(game)
    loaded = persistency.load_game(game.savepath)
    assert loaded is not None
    Migrator(loaded, is_liberation=False)
    return loaded


def generate_starting_mission(game: Game, folder: Path) -> MissionSimulation:
    """Writes the mission as Begin Campaign does, parked aircraft and all."""
    sim = MissionSimulation(game)
    sim.begin_simulation()
    sim.generate_miz(folder / "start.miz")
    return sim


# The link: the companion's data port.

TOKEN = "tok-1"

#: Ends the stream connection that reads it.
CLOSE = object()

#: What a stream connection sends: event text, CLOSE, or an Event to wait for.
Item = Union[str, object, threading.Event]


class FakeCompanion:
    """The companion's data port: hello, the stream (one script per stream
    connection) and the event log's `entries`, recording each `after` and
    `limit` asked."""

    def __init__(self, scripts: Optional[list[list[Item]]] = None) -> None:
        self.scripts: list[list[Item]] = scripts or []
        self.entries: list[dict[str, Any]] = []
        self.afters: list[int] = []
        self.limits: list[int] = []
        self.closing = threading.Event()
        self.lock = threading.Lock()
        self.streams = 0
        self.refused = 0
        #: How many hellos, from the first, leave out `lastSeq`.
        self.hellos_without_last = 0
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self, port: int = 0) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    with fake.lock:
                        fake.refused += 1
                    self.answer(401, {"error": "unauthorized"})
                    return
                url = urlsplit(self.path)
                if url.path == "/v1/hello":
                    hello: dict[str, Any] = {
                        "protocol": 1,
                        "boot": "b1",
                        "session": 1,
                        "modelTime": 5,
                        "lastSeq": fake.last_seq(),
                    }
                    with fake.lock:
                        if fake.hellos_without_last > 0:
                            fake.hellos_without_last -= 1
                            del hello["lastSeq"]
                    self.answer(200, hello)
                elif url.path == "/v1/events":
                    query = parse_qs(url.query)
                    after = int(query.get("after", ["0"])[0])
                    limit = int(query.get("limit", ["500"])[0])
                    with fake.lock:
                        fake.afters.append(after)
                        fake.limits.append(limit)
                        page = [e for e in fake.entries if e["seq"] > after][:limit]
                    self.answer(
                        200,
                        {
                            "boot": "b1",
                            "session": 1,
                            "modelTime": 5,
                            "lastSeq": fake.last_seq(),
                            "entries": page,
                        },
                    )
                elif url.path == "/v1/stream":
                    with fake.lock:
                        n = fake.streams
                        fake.streams += 1
                    script = fake.scripts[n] if n < len(fake.scripts) else []
                    self.stream(script)
                else:
                    self.answer(404, {"error": "not found"})

            def answer(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                if status == 401:
                    self.send_header("WWW-Authenticate", "Bearer")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def stream(self, script: list[Item]) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for item in script:
                        if item is CLOSE:
                            return
                        if isinstance(item, threading.Event):
                            while not item.wait(0.02):
                                if fake.closing.is_set():
                                    return
                            continue
                        assert isinstance(item, str)
                        self.wfile.write(item.encode())
                        self.wfile.flush()
                    fake.closing.wait()
                except OSError:
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server.daemon_threads = False
        self.server = server
        self.thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    def last_seq(self) -> int:
        with self.lock:
            return self.entries[-1]["seq"] if self.entries else 0

    def add(self, *seqs: int) -> None:
        with self.lock:
            self.entries.extend(entry(n) for n in seqs)

    @property
    def address(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.closing.set()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join()


def event(kind: str, data: str) -> str:
    return f"event: {kind}\ndata: {data}\n\n"


def entry(seq: int, kind: str = "dead", **extra: Any) -> dict[str, Any]:
    e: dict[str, Any] = {
        "seq": seq,
        "kind": kind,
        "boot": "b1",
        "session": 1,
        "modelTime": 2334.5 + seq,
        "time": "2026-11-21T19:40:12.345Z",
        "hookN": seq,
        "unit": {"name": f"Tank-{seq}", "type": "T-55", "side": 1},
        "pos": {"x": 100.0, "y": 200.0, "alt": 12.3, "lat": 26.1, "lon": 56.2},
    }
    e.update(extra)
    return e


@pytest.fixture
def fakes() -> Iterator[list[FakeCompanion]]:
    made: list[FakeCompanion] = []
    yield made
    for fake in made:
        fake.close()


@pytest.fixture
def serve(
    fakes: list[FakeCompanion],
) -> Callable[..., FakeCompanion]:
    def start(*scripts: list[Item], port: int = 0) -> FakeCompanion:
        fake = FakeCompanion(list(scripts))
        fake.start(port)
        fakes.append(fake)
        return fake

    return start


# The command port.

COMMAND_TOKEN = "tok-c"


class FakeCommands:
    """The companion's command port: spawn requests and their records."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[tuple[dict[str, Any], dict[str, str]]] = []
        #: The answer to the next POST /v1/spawn, or None for 202 s-1.
        self.refuse: Optional[tuple[int, str]] = None
        #: The statuses GET /v1/spawns/s-1 gives, in turn; the last repeats.
        #: "http:503" answers that HTTP status with an error instead.
        self.statuses: list[str] = []
        #: The groups POST /v1/despawn removes, with their units; others 404.
        self.groups: dict[str, int] = {}
        #: The status every POST /v1/despawn answers with an error, or None.
        self.despawn_refuse: Optional[int] = None
        #: The bodies and headers of POST /v1/despawn, in order.
        self.despawns: list[tuple[dict[str, Any], dict[str, str]]] = []
        #: The spawns GET /v1/spawns lists, as (name, status).
        self.spawns: list[tuple[str, str]] = []
        #: The headers of GET /v1/spawns, in order.
        self.lists: list[dict[str, str]] = []
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def authorized(self) -> bool:
                if self.headers.get("Authorization") == f"Bearer {COMMAND_TOKEN}":
                    return True
                self.answer(401, {"error": "unauthorized"})
                return False

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if not self.authorized():
                    return
                if self.path == "/v1/despawn":
                    self.despawn(json.loads(raw))
                    return
                if self.path != "/v1/spawn":
                    self.answer(404, {"error": "not found"})
                    return
                with fake.lock:
                    fake.requests.append((json.loads(raw), dict(self.headers)))
                    refuse = fake.refuse
                if refuse is not None:
                    self.answer(refuse[0], {"error": refuse[1]})
                    return
                self.answer(202, {"id": "s-1"})

            def do_GET(self) -> None:
                if not self.authorized():
                    return
                if self.path == "/v1/spawns":
                    self.list_spawns()
                    return
                if self.path != "/v1/spawns/s-1":
                    self.answer(404, {"error": "no spawn s-2"})
                    return
                with fake.lock:
                    status = (
                        fake.statuses.pop(0)
                        if len(fake.statuses) > 1
                        else fake.statuses[0]
                    )
                if status.startswith("http:"):
                    self.answer(int(status[5:]), {"error": "not now"})
                    return
                spawn: dict[str, Any] = {
                    "id": "s-1",
                    "name": "LINK 1",
                    "country": 2,
                    "category": "plane",
                    "mission": {"boot": "b1", "session": 1},
                    "status": status,
                    "asked": "2026-11-22T10:00:00.000Z",
                }
                if status == "failed":
                    spawn["reason"] = "campaign_spawn: group name in use"
                self.answer(
                    200,
                    {"boot": "b1", "session": 1, "modelTime": 2401.2, "spawn": spawn},
                )

            def list_spawns(self) -> None:
                with fake.lock:
                    fake.lists.append(dict(self.headers))
                    spawns = [
                        {
                            "id": f"s-{i}",
                            "name": name,
                            "country": 2,
                            "category": "plane",
                            "mission": {"boot": "b1", "session": 1},
                            "status": status,
                            "asked": "2026-11-22T09:14:00.000Z",
                        }
                        for i, (name, status) in enumerate(fake.spawns, start=1)
                    ]
                self.answer(
                    200,
                    {"boot": "b1", "session": 1, "modelTime": 2401.2, "spawns": spawns},
                )

            def despawn(self, body: dict[str, Any]) -> None:
                with fake.lock:
                    fake.despawns.append((body, dict(self.headers)))
                    refuse = fake.despawn_refuse
                    name = str(body.get("name"))
                    units = None if refuse else fake.groups.pop(name, None)
                if refuse is not None:
                    self.answer(refuse, {"error": "hook link down"})
                    return
                if units is None:
                    self.answer(404, {"error": f"campaign_despawn: no group {name}"})
                    return
                self.answer(
                    200,
                    {
                        "boot": "b1",
                        "session": 1,
                        "modelTime": 2410.5,
                        "group": name,
                        "units": units,
                    },
                )

            def answer(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                if status == 401:
                    self.send_header("WWW-Authenticate", "Bearer")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server = server
        self.thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    @property
    def address(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join()


# Waiting.


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Polls `predicate` until it holds; fails the test after `timeout` s."""
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.01)
