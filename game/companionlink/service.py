"""DCS Companion's service, as Retribution sees and controls it.

On lake the companion runs as the s6 service `svc-companion` of the `dcs`
container, from its folder (`home`, `/config/dcs-companion`): the service
writes the running companion's pid to `companion.pid`, appends its output to
`companion.log`, and does not start it while the file `off` exists. A
companion that exits is started again by s6, so a restart is a SIGTERM to that
pid (SIGKILL after `grace` seconds). Retribution runs as the same user as the
companion, so no root and no companion endpoint are needed. A pid is signalled
only when its command line names a file called `dcs-companion`, so a stale pid
file never hits another process.

The status comes from the data port's `GET /v1/hello` (`linked`, `session`)
and the files. Where `home` is not a folder (Retribution on Windows, or a
companion run elsewhere) the service is not managed: only the hello counts,
and the file actions do nothing.

Every call blocks (the menu runs them off the GUI thread).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("game.companionlink")

#: The companion's file name, as its command line names it.
BINARY = "dcs-companion"


class ServiceState(Enum):
    LINKED = "linked"
    NO_DCS = "no_dcs"
    STARTING = "starting"
    OFF = "off"
    NOT_ANSWERING = "not_answering"


@dataclass(frozen=True)
class ServiceStatus:
    state: ServiceState
    #: The hello's `session` (the mission's number in this DCS run) when linked.
    session: int = 0
    #: The companion's folder is here: Restart, Turn off and on, Show log.
    managed: bool = False

    def text(self) -> str:
        if self.state is ServiceState.LINKED:
            return f"running (DCS linked, mission {self.session})"
        return {
            ServiceState.NO_DCS: "running (no DCS)",
            ServiceState.STARTING: "starting",
            ServiceState.OFF: "turned off",
            ServiceState.NOT_ANSWERING: "not answering",
        }[self.state]


class CompanionService:
    def __init__(
        self,
        home: Path,
        address: str,
        token: str,
        *,
        grace: float = 10.0,
        start_window: float = 30.0,
        timeout: float = 2.0,
    ) -> None:
        self.home = home
        self.address = address.rstrip("/")
        self.token = token
        self.grace = grace
        self.start_window = start_window
        self.timeout = timeout

    @property
    def managed(self) -> bool:
        return self.home.is_dir()

    def status(self) -> ServiceStatus:
        managed = self.managed
        if managed and (self.home / "off").exists():
            return ServiceStatus(ServiceState.OFF, managed=managed)
        hello = self._hello()
        if hello is not None:
            if hello.get("linked") is True:
                session = hello.get("session")
                return ServiceStatus(
                    ServiceState.LINKED,
                    session=session if isinstance(session, int) else 0,
                    managed=managed,
                )
            return ServiceStatus(ServiceState.NO_DCS, managed=managed)
        if managed:
            pid = self._pid()
            age = self._pid_age()
            if pid is not None and age is not None and age < self.start_window:
                if _is_companion(pid):
                    return ServiceStatus(ServiceState.STARTING, managed=managed)
        return ServiceStatus(ServiceState.NOT_ANSWERING, managed=managed)

    def restart(self) -> bool:
        """Stops the running companion; s6 starts it again. False when no
        companion runs under the pid file's pid."""
        pid = self._pid()
        if pid is None or not _is_companion(pid):
            log.info("companion restart: no companion runs (pid file: %s)", pid)
            return False
        log.info("companion restart: SIGTERM to pid %d", pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        end = time.monotonic() + self.grace
        while time.monotonic() < end:
            if not _is_companion(pid):
                log.info("companion pid %d stopped", pid)
                return True
            time.sleep(0.1)
        log.info("companion pid %d still runs after %.1f s: SIGKILL", pid, self.grace)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return True

    def turn_off(self) -> None:
        """Keeps the service from starting the companion, and stops it."""
        if not self.managed:
            return
        (self.home / "off").touch()
        log.info("companion turned off")
        self.restart()

    def turn_on(self) -> None:
        if not self.managed:
            return
        (self.home / "off").unlink(missing_ok=True)
        log.info("companion turned on")

    def log_tail(self, lines: int = 500) -> str:
        path = self.home / "companion.log"
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                chunk = 64 * 1024
                data = b""
                pos = size
                while pos > 0 and data.count(b"\n") <= lines:
                    step = min(chunk, pos)
                    pos -= step
                    f.seek(pos)
                    data = f.read(step) + data
        except OSError:
            return ""
        kept = data.decode("utf-8", errors="replace").splitlines()[-lines:]
        return "".join(line + "\n" for line in kept)

    def _hello(self) -> Optional[dict[str, object]]:
        try:
            resp = httpx.get(
                f"{self.address}/v1/hello",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                return None
            hello = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        return hello if isinstance(hello, dict) else None

    def _pid(self) -> Optional[int]:
        try:
            return int((self.home / "companion.pid").read_text().strip())
        except (OSError, ValueError):
            return None

    def _pid_age(self) -> Optional[float]:
        try:
            return time.time() - (self.home / "companion.pid").stat().st_mtime
        except OSError:
            return None


def _is_companion(pid: int) -> bool:
    """Whether `pid` is a live process whose command line names a file called
    `dcs-companion` as its first or second word (`./dcs-companion run …`, or
    a script run by a shell). An exited process not yet reaped is not."""
    try:
        out = subprocess.run(
            ["ps", "-o", "args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    words = out.split()
    return any(Path(w).name == BINARY for w in words[:2])
