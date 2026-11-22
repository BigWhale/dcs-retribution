"""The client of DCS Companion's command port: spawn requests and their records.

`POST /v1/spawn` asks the companion to spawn a group (its pydcs table) now or
at a model time; the companion answers `202 {"id":"s-1"}` and spawns it when
it is due. `GET /v1/spawns/{id}` gives the spawn's record, whose `status`
tells what became of it. Errors come as `{"error": "..."}` with a fitting
status; a wrong token is `401`.

Every call blocks (the launcher runs them on a worker thread) and fails with
`CommandError`, whose message never holds the token.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

log = logging.getLogger("game.companionlink")

#: A spawn's statuses that are final and not `spawned`.
_FINAL_FAILED = ("failed", "despawned")


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

    def __str__(self) -> str:
        if self.status is None:
            return self.message
        return f"the companion answered {self.status}: {self.message}"


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

    def spawn_status(self, spawn_id: str) -> str:
        """The spawn's status: waiting, unknown, spawned, failed or despawned."""
        return str(self._record(spawn_id).get("status"))

    def wait_spawned(
        self, spawn_id: str, limit: float = 30.0, poll: float = 0.5
    ) -> None:
        """Returns once the spawn is `spawned`.

        Raises CommandError when it is `failed` or `despawned` (with the
        companion's reason), or still not spawned after `limit` seconds
        (status None).
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
                raise CommandError(None, self._clean(message))
            left = deadline - time.monotonic()
            if left <= 0:
                raise CommandError(
                    None,
                    f"spawn {spawn_id} not spawned within {limit:g} s "
                    f"(still {status})",
                )
            time.sleep(min(poll, left))

    def _record(self, spawn_id: str) -> dict[str, Any]:
        resp = self._send("GET", f"/v1/spawns/{spawn_id}")
        if resp.status_code != 200:
            raise self._refusal(resp)
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

    def _refusal(self, resp: httpx.Response) -> CommandError:
        try:
            error = resp.json().get("error")
        except (ValueError, AttributeError):
            error = None
        if not isinstance(error, str) or not error:
            error = resp.text[:200] or resp.reason_phrase
        if resp.status_code == 401:
            error = f"the companion refused the command token: {error}"
        error = self._clean(error)
        log.warning("companion command refused (%d): %s", resp.status_code, error)
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
