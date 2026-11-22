"""The dynamic campaign's module loggers survive the app's logging setup.

`init_logging` runs dictConfig after the modules are imported; a logger the
config does not name (or cover by a named parent) is disabled, and its lines
never reach the log.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

import pytest

import game.companionlink
import qt_ui
from game.logging_config import init_logging

REPO = Path(__file__).resolve().parent.parent


def modules(package: ModuleType) -> list[ModuleType]:
    """Every module of the package, imported."""
    return [
        importlib.import_module(info.name)
        for info in pkgutil.walk_packages(package.__path__, package.__name__ + ".")
    ]


#: Where the live campaign's code lives: the link's package, and the GUI's
#: controller of it that other live modules build on.
LINK_SOURCES = ("game.companionlink", "qt_ui.companionlinkcontroller")


def uses_the_link(module: ModuleType) -> bool:
    """Whether the module takes anything from the link or its controller."""
    for value in vars(module).values():
        source = (
            value.__name__
            if isinstance(value, ModuleType)
            else getattr(value, "__module__", None)
        )
        if isinstance(source, str) and source.startswith(LINK_SOURCES):
            return True
    return False


#: The live campaign's modules: the link's package and the GUI modules that
#: use it.
LIVE_MODULES = [game.companionlink, *modules(game.companionlink)] + [
    m for m in modules(qt_ui) if uses_the_link(m)
]

#: The loggers the live campaign's modules create on import.
LOGGERS = sorted(
    {
        value.name
        for module in LIVE_MODULES
        for value in vars(module).values()
        if isinstance(value, logging.Logger)
    }
)


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
    assert "game.companionlink" in LOGGERS
    assert any(name.startswith("qt_ui.") for name in LOGGERS)
    assert "qt_ui.dynamicautosave" in LOGGERS
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
