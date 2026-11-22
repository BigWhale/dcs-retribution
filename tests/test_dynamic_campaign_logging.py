"""The dynamic campaign's module loggers survive the app's logging setup.

`init_logging` runs dictConfig after the modules are imported; a logger the
config does not name (or cover by a named parent) is disabled, and its lines
never reach the log.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterator

import pytest

import game.companionlink.link  # noqa: F401  (creates its logger on import)
import qt_ui.companionlinkcontroller  # noqa: F401
import qt_ui.flightlauncher  # noqa: F401
import qt_ui.livelosses  # noqa: F401
from game.logging_config import init_logging

REPO = Path(__file__).resolve().parent.parent

#: The loggers the dynamic campaign's modules create on import.
LOGGERS = [
    "game.companionlink",
    "qt_ui.companionlinkcontroller",
    "qt_ui.flightlauncher",
    "qt_ui.livelosses",
]


@pytest.fixture
def app_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Runs init_logging as the app does, from a tmp dir holding the repo's
    default config; restores every logger afterwards."""
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources" / "default_logging.yaml").symlink_to(
        REPO / "resources" / "default_logging.yaml"
    )
    monkeypatch.chdir(tmp_path)
    root = logging.getLogger()
    saved_root = (root.level, list(root.handlers))
    saved: dict[str, tuple[Any, ...]] = {
        name: (lg.disabled, lg.level, list(lg.handlers), lg.propagate)
        for name, lg in logging.root.manager.loggerDict.items()
        if isinstance(lg, logging.Logger)
    }
    try:
        init_logging("test")
        yield tmp_path / "logs" / "retribution.log"
    finally:
        for handler in root.handlers:
            if handler not in saved_root[1]:
                handler.close()
        root.setLevel(saved_root[0])
        root.handlers[:] = saved_root[1]
        for name, lg in logging.root.manager.loggerDict.items():
            if not isinstance(lg, logging.Logger):
                continue
            if name in saved:
                lg.disabled, lg.level, handlers, lg.propagate = saved[name]
                lg.handlers[:] = handlers
            else:
                lg.disabled = False


def test_live_campaign_loggers_reach_the_log(app_logging: Path) -> None:
    for name in LOGGERS:
        logger = logging.getLogger(name)
        assert logger.disabled is False, name
        assert logger.isEnabledFor(logging.INFO), name
        logger.info("%s says hello", name)
    for handler in logging.getLogger().handlers:
        handler.flush()

    written = app_logging.read_text(encoding="utf-8")
    for name in LOGGERS:
        assert f":: {name} :: INFO :: {name} says hello" in written
