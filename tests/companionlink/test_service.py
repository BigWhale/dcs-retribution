"""The companion's service on lake, as Retribution sees and controls it.

The companion's folder (`home`, `/config/dcs-companion` on lake) is a temporary
folder here. Its data port is the link fake (`FakeCompanion`, in conftest), or
a closed port. The running companion is a shell script named `dcs-companion`,
started for real, its pid in `companion.pid` as the service writes it: a
polite one stops on SIGTERM, a stubborn one ignores it.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
from pathlib import Path
from typing import Callable, Iterator

import pytest

from game.companionlink.service import CompanionService, ServiceState
from tests.conftest import TOKEN, FakeCompanion, wait_until

POLITE = "trap 'exit 0' TERM\ntouch \"$1\"\nwhile :; do sleep 0.1; done\n"
STUBBORN = "trap '' TERM\ntouch \"$1\"\nwhile :; do sleep 0.1; done\n"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "dcs-companion"
    h.mkdir()
    return h


@pytest.fixture
def processes() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for p in started:
        if p.poll() is None:
            p.kill()
            p.wait()


@pytest.fixture
def companion(
    tmp_path: Path, home: Path, processes: list[subprocess.Popen[bytes]]
) -> Callable[[str], subprocess.Popen[bytes]]:
    """Starts a stand-in companion with `body`, once its trap is set (it
    touches the file named by its argument), and writes its pid file."""

    def start(body: str) -> subprocess.Popen[bytes]:
        script = tmp_path / "bin" / "dcs-companion"
        script.parent.mkdir(exist_ok=True)
        script.write_text(body)
        ready = tmp_path / "ready"
        ready.unlink(missing_ok=True)
        p = subprocess.Popen(["/bin/sh", str(script), str(ready)])
        processes.append(p)
        wait_until(ready.exists)  # its trap is set
        (home / "companion.pid").write_text(f"{p.pid}\n")
        return p

    return start


def closed_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


def linked_fake(serve: Callable[..., FakeCompanion], session: int = 3) -> FakeCompanion:
    fake = serve()
    fake.linked = True
    fake.session = session
    return fake


def alive(p: subprocess.Popen[bytes]) -> bool:
    return p.poll() is None


def test_status_linked(home: Path, serve: Callable[..., FakeCompanion]) -> None:
    fake = linked_fake(serve)
    status = CompanionService(home, fake.address, TOKEN).status()
    assert status.state is ServiceState.LINKED
    assert status.text() == "running (DCS linked, mission 3)"
    assert status.managed


def test_status_no_dcs(home: Path, serve: Callable[..., FakeCompanion]) -> None:
    fake = linked_fake(serve)
    fake.linked = False
    status = CompanionService(home, fake.address, TOKEN).status()
    assert status.text() == "running (no DCS)"


def test_status_off_wins(home: Path, serve: Callable[..., FakeCompanion]) -> None:
    fake = linked_fake(serve)
    (home / "off").touch()
    status = CompanionService(home, fake.address, TOKEN).status()
    assert status.state is ServiceState.OFF
    assert status.text() == "turned off"


def test_status_starting_then_not_answering(
    home: Path, companion: Callable[[str], subprocess.Popen[bytes]]
) -> None:
    companion(POLITE)
    address = closed_port()
    assert CompanionService(home, address, TOKEN).status().text() == "starting"
    late = CompanionService(home, address, TOKEN, start_window=0)
    assert late.status().text() == "not answering"


def test_stale_pid(home: Path, processes: list[subprocess.Popen[bytes]]) -> None:
    other = subprocess.Popen(["sleep", "60"])
    processes.append(other)
    (home / "companion.pid").write_text(f"{other.pid}\n")
    service = CompanionService(home, closed_port(), TOKEN)
    assert service.status().text() == "not answering"
    assert service.restart() is False
    assert alive(other)


def test_restart_polite(
    home: Path,
    companion: Callable[[str], subprocess.Popen[bytes]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    p = companion(POLITE)
    with caplog.at_level(logging.INFO, logger="game.companionlink"):
        assert CompanionService(home, closed_port(), TOKEN).restart() is True
    p.wait(timeout=5)
    assert "SIGKILL" not in caplog.text
    assert str(p.pid) in caplog.text


def test_restart_stubborn(
    home: Path,
    companion: Callable[[str], subprocess.Popen[bytes]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    p = companion(STUBBORN)
    service = CompanionService(home, closed_port(), TOKEN, grace=0.5)
    with caplog.at_level(logging.INFO, logger="game.companionlink"):
        assert service.restart() is True
    p.wait(timeout=5)
    assert "SIGKILL" in caplog.text


def test_turn_off_and_on(
    home: Path, companion: Callable[[str], subprocess.Popen[bytes]]
) -> None:
    p = companion(POLITE)
    service = CompanionService(home, closed_port(), TOKEN)
    service.turn_off()
    assert (home / "off").exists()
    p.wait(timeout=5)
    service.turn_on()
    assert not (home / "off").exists()


def test_not_managed(tmp_path: Path, serve: Callable[..., FakeCompanion]) -> None:
    fake = linked_fake(serve)
    service = CompanionService(tmp_path / "missing", fake.address, TOKEN)
    assert not service.managed
    status = service.status()
    assert status.text() == "running (DCS linked, mission 3)"
    assert not status.managed


def test_log_tail(home: Path) -> None:
    service = CompanionService(home, closed_port(), TOKEN)
    assert service.log_tail() == ""
    (home / "companion.log").write_text("".join(f"line {n}\n" for n in range(1, 601)))
    tail = service.log_tail()
    assert tail.splitlines()[0] == "line 101"
    assert tail.splitlines()[-1] == "line 600"
    assert len(tail.splitlines()) == 500


def test_pid_file_of_a_dead_process(home: Path) -> None:
    (home / "companion.pid").write_text(f"{2**22 + os.getpid()}\n")
    service = CompanionService(home, closed_port(), TOKEN)
    assert service.status().text() == "not answering"
    assert service.restart() is False
