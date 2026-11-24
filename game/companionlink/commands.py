"""The client of DCS Companion's command port: spawn requests and their records.

`POST /v1/spawn` asks the companion to spawn a group (its pydcs table) now or
at a model time; the companion answers `202 {"id":"s-1"}` and spawns it when
it is due, or `409` when a spawn of the mission still holds the name.
`GET /v1/spawns` lists the mission's spawns (whose names are held while one is
`waiting`, `unknown` or `spawned`), `GET /v1/spawns/{id}` gives one spawn's
record, whose `status` tells what became of it. `POST /v1/despawn` removes a group of the mission
now (`200` with its `units`, `404` when there is no such group).
`POST /v1/mission/load` asks the server to load a mission from its `Missions/`
(`202`; the load itself is not awaited). Errors come as `{"error": "..."}` with
a fitting status; a wrong token is `401`.

Every call blocks (the launcher runs them on a worker thread) and fails with
`CommandError`, whose message never holds the token.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import httpx

from game.companionlink.linkstatus import LinkStatus

log = logging.getLogger("game.companionlink")

#: A spawn's statuses that are final and not `spawned`.
_FINAL_FAILED = ("failed", "despawned")
#: A spawn's statuses that hold its name in the mission: the companion
#: refuses another spawn of that name (`409`).
_HOLDING = ("waiting", "unknown", "spawned")
#: A spawn's id: `s-` and its number.
_SPAWN_ID = re.compile(r"s-([0-9]+)")


@dataclass(frozen=True)
class SpawnsView:
    """`GET /v1/spawns`: the mission the companion saw last and its spawns."""

    boot: str
    session: int
    model_time: float
    #: Each spawn's id number (`s-12` is 12) and status, in id order.
    spawns: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class SpawnRequest:
    name: str
    country: int
    category: str  # "plane" | "helicopter"
    group: dict[str, Any]  # the pydcs table, ready to send
    at: float | None = None  # None: at once

    def body(self) -> bytes:
        """The JSON body of `POST /v1/spawn`; `at` only when set."""
        body: dict[str, Any] = {
            "name": self.name,
            "country": self.country,
            "category": self.category,
            "group": self.group,
        }
        if self.at is not None:
            body["at"] = self.at
        return json.dumps(body, separators=(",", ":")).encode("utf-8")


class CommandError(Exception):
    """A command the companion refused, or could not be asked."""

    def __init__(self, status: Optional[int], message: str) -> None:
        super().__init__(message)
        #: The HTTP status; None for a connect error or a timeout.
        self.status = status
        #: The companion's `error`, or what went wrong; never the token.
        self.message = message
        #: The step of a launch that failed (`despawn of 'Spare 1'`), if set.
        self.step: Optional[str] = None
        #: What the launch had done before the step failed, if anything.
        self.done: Optional[str] = None

    def __str__(self) -> str:
        if self.status is None:
            text = self.message
        else:
            text = f"the companion answered {self.status}: {self.message}"
        if self.step is not None:
            text = f"{self.step} failed: {text}"
        if self.done is not None:
            text = f"{text}; {self.done}"
        return text


class SpawnFailed(CommandError):
    """A spawn the companion gave up on: its status is `failed` or `despawned`."""


class SpawnPending(CommandError):
    """A spawn not yet `spawned` when the wait ran out; it may still spawn."""

    def __init__(self, message: str, last_status: Optional[str]) -> None:
        super().__init__(None, message)
        #: The spawn's status at the last look ("waiting", "unknown", ...).
        self.last_status = last_status


class CompanionCommands:
    def __init__(self, address: str, token: str, timeout: float = 10.0) -> None:
        self._address = address.rstrip("/")
        self._token = token
        self._timeout = timeout

    def spawn(self, request: SpawnRequest) -> str:
        """Asks for a spawn; its id ("s-1")."""
        body = request.body()
        resp = self._send(
            "POST",
            "/v1/spawn",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 202:
            raise self._refusal(resp)
        spawn_id = self._json(resp).get("id")
        if not isinstance(spawn_id, str) or not spawn_id:
            raise CommandError(resp.status_code, "the spawn answer has no id")
        log.info(
            "companion spawn asked: %s %r, at %s, %d bytes",
            spawn_id,
            request.name,
            "once" if request.at is None else request.at,
            len(body),
        )
        return spawn_id

    def despawn(self, name: str) -> int | None:
        """Removes the group `name` from the mission now; its units removed.

        None when the mission has no such group (`404`): it is gone already.
        Any other answer (`409` refused, `503` link down, `504` no answer in
        time) or no answer at all raises CommandError: the group may still
        be there.
        """
        resp = self._send(
            "POST",
            "/v1/despawn",
            content=json.dumps({"name": name}, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code == 404:
            log.info("companion despawn: no group %r, gone already", name)
            return None
        if resp.status_code != 200:
            raise self._refusal(resp)
        units = self._json(resp).get("units")
        if not isinstance(units, int) or isinstance(units, bool):
            raise CommandError(resp.status_code, "the despawn answer has no units")
        log.info("companion despawned: %r, %d units", name, units)
        return units

    def held_names(self) -> frozenset[str]:
        """The group names the companion holds in its mission: those of its
        spawns `waiting`, `unknown` or `spawned` (`GET /v1/spawns`).

        A spawn of one of them is refused (`409`), even when no group of that
        name is in the mission any more. Raises CommandError when the
        companion cannot be asked or refuses.
        """
        resp = self._send("GET", "/v1/spawns")
        if resp.status_code != 200:
            raise self._refusal(resp, quiet=True)
        spawns = self._json(resp).get("spawns")
        if not isinstance(spawns, list):
            raise CommandError(resp.status_code, "the spawns answer has no spawns")
        return frozenset(
            s["name"]
            for s in spawns
            if isinstance(s, dict)
            and s.get("status") in _HOLDING
            and isinstance(s.get("name"), str)
        )

    def spawns(self) -> SpawnsView:
        """The mission's spawns as the companion sees them (`GET /v1/spawns`).

        Raises CommandError when the companion cannot be asked, refuses, or
        answers with something that is not the spawns' view.
        """
        resp = self._send("GET", "/v1/spawns")
        if resp.status_code != 200:
            raise self._refusal(resp, quiet=True)
        body = self._json(resp)
        boot = body.get("boot")
        session = body.get("session")
        model_time = body.get("modelTime")
        spawns = body.get("spawns")
        if (
            not isinstance(boot, str)
            or not isinstance(session, int)
            or isinstance(session, bool)
            or not isinstance(model_time, (int, float))
            or isinstance(model_time, bool)
            or not isinstance(spawns, list)
        ):
            raise CommandError(resp.status_code, "the spawns answer is not a view")
        entries: list[tuple[int, str]] = []
        for s in spawns:
            spawn_id = s.get("id") if isinstance(s, dict) else None
            status = s.get("status") if isinstance(s, dict) else None
            found = _SPAWN_ID.fullmatch(spawn_id) if isinstance(spawn_id, str) else None
            if found is None or not isinstance(status, str):
                raise CommandError(
                    resp.status_code,
                    "the spawns answer has a spawn without id or status",
                )
            entries.append((int(found.group(1)), status))
        return SpawnsView(boot, session, float(model_time), tuple(entries))

    def load_mission(self, file: str) -> None:
        """Asks the companion to load `file` from the server's `Missions/`.

        Returns once the companion has asked the hook (`202`); the load itself
        follows, seen as a new session. Any other answer (`400` bad name, `404`
        no such file, `502` refused by the hook, `503` link down, `504` no
        answer in time) or no answer raises CommandError with the companion's
        text.
        """
        resp = self._send(
            "POST",
            "/v1/mission/load",
            content=json.dumps({"file": file}, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 202:
            raise self._refusal(resp)
        log.info("companion mission load asked: %s", file)

    def reachable(self) -> LinkStatus:
        """Whether the command port answers (`GET /v1/spawns`): LINKED on 200,
        REFUSED when it refuses the token (401), WAITING otherwise or when it
        does not answer at all."""
        try:
            resp = self._send("GET", "/v1/spawns")
        except CommandError:
            return LinkStatus.WAITING
        if resp.status_code == 200:
            return LinkStatus.LINKED
        if resp.status_code == 401:
            return LinkStatus.REFUSED
        return LinkStatus.WAITING

    def spawn_status(self, spawn_id: str) -> str:
        """The spawn's status: waiting, unknown, spawned, failed or despawned."""
        return str(self._record(spawn_id).get("status"))

    def wait_spawned(
        self, spawn_id: str, limit: float = 30.0, poll: float = 0.5
    ) -> None:
        """Returns once the spawn is `spawned`.

        Raises SpawnFailed when it is `failed` or `despawned` (with the
        companion's reason), SpawnPending when it is still not spawned after
        `limit` seconds, CommandError when the companion cannot be asked or
        refuses; all of them CommandError, the first two with status None.
        """
        deadline = time.monotonic() + limit
        while True:
            record = self._record(spawn_id)
            status = record.get("status")
            if status == "spawned":
                log.info("companion spawned: %s", spawn_id)
                return
            if status in _FINAL_FAILED:
                reason = record.get("reason")
                message = f"spawn {spawn_id} {status}"
                if reason:
                    message += f": {reason}"
                raise SpawnFailed(None, self._clean(message))
            left = deadline - time.monotonic()
            if left <= 0:
                raise SpawnPending(
                    f"spawn {spawn_id} not spawned within {limit:g} s "
                    f"(still {status})",
                    None if status is None else str(status),
                )
            time.sleep(min(poll, left))

    def _record(self, spawn_id: str) -> dict[str, Any]:
        resp = self._send("GET", f"/v1/spawns/{spawn_id}")
        if resp.status_code != 200:
            raise self._refusal(resp, quiet=True)
        spawn = self._json(resp).get("spawn")
        if not isinstance(spawn, dict):
            raise CommandError(resp.status_code, "the spawn answer has no spawn")
        log.debug("companion spawn %s: %s", spawn_id, spawn.get("status"))
        return spawn

    def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}"}
        headers.update(kw.pop("headers", {}))
        try:
            with httpx.Client(timeout=self._timeout) as client:
                return client.request(
                    method, f"{self._address}{path}", headers=headers, **kw
                )
        except httpx.TimeoutException as e:
            raise CommandError(
                None,
                self._clean(f"the companion went silent ({type(e).__name__})"),
            ) from None
        except httpx.HTTPError as e:
            raise CommandError(
                None,
                self._clean(
                    f"cannot reach the companion at {self._address}: "
                    f"{type(e).__name__}: {e}"
                ),
            ) from None

    def _refusal(self, resp: httpx.Response, quiet: bool = False) -> CommandError:
        """The error for a refused command; logged at warning, or at debug
        when `quiet` (a spawn's record, asked again and again while it is
        followed: the caller logs what matters)."""
        try:
            error = resp.json().get("error")
        except (ValueError, AttributeError):
            error = None
        if not isinstance(error, str) or not error:
            error = resp.text[:200] or resp.reason_phrase
        if resp.status_code == 401:
            error = f"the companion refused the command token: {error}"
        error = self._clean(error)
        log.log(
            logging.DEBUG if quiet else logging.WARNING,
            "companion command refused (%d): %s",
            resp.status_code,
            error,
        )
        return CommandError(resp.status_code, error)

    def _json(self, resp: httpx.Response) -> dict[str, Any]:
        try:
            body = resp.json()
        except ValueError:
            raise CommandError(resp.status_code, "the companion's answer is not JSON")
        if not isinstance(body, dict):
            raise CommandError(
                resp.status_code, "the companion's answer is not an object"
            )
        return body

    def _clean(self, message: str) -> str:
        """The message with the token taken out, should it ever be in it."""
        if self._token:
            return message.replace(self._token, "<token>")
        return message


def despawn_and_spawn(
    commands: CompanionCommands, despawn: Sequence[str], request: SpawnRequest
) -> str:
    """Despawns each group of `despawn` in order, then asks for the spawn; its id.

    The spare planes on a ground start's spots go before the flight is spawned
    on them. A group already gone is fine; any other despawn failure raises
    its CommandError before the spawn is asked, so nothing is spawned over a
    plane that may still stand there.

    The error says which step failed: `despawn of 'Spare 1' failed: ...` or
    `spawn of 'Uzi 1' failed: ...`, and which spares are gone already when
    the spawn failed after them (`...; the parked spares 'Spare 1' were
    despawned for it`).
    """
    for name in despawn:
        try:
            commands.despawn(name)
        except CommandError as e:
            e.step = f"despawn of {name!r}"
            raise
    try:
        return commands.spawn(request)
    except CommandError as e:
        e.step = f"spawn of {request.name!r}"
        if despawn:
            spares = ", ".join(repr(n) for n in despawn)
            e.done = f"the parked spares {spares} were despawned for it"
        raise
