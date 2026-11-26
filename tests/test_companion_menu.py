"""The Companion menu, on an offscreen Qt application.

The menu's service is a real `CompanionService` on a temporary folder and the
link fake (`FakeCompanion`, in conftest) as the data port. What is checked is
what the menu shows and what Turn off leaves in the folder.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

from game.companionlink.service import CompanionService  # noqa: E402
from qt_ui.companionmenu import CompanionMenu  # noqa: E402
from tests.conftest import TOKEN, FakeCompanion, wait_until  # noqa: E402


@pytest.fixture(scope="module")
def app() -> QApplication:
    existing = QApplication.instance()
    return existing if isinstance(existing, QApplication) else QApplication([])


@pytest.fixture
def parent(app: QApplication) -> QWidget:
    """The menu's parent, kept alive for the test (it owns the menu)."""
    return QWidget()


def linked_fake(serve: Callable[..., FakeCompanion]) -> FakeCompanion:
    fake = serve()
    fake.linked = True
    fake.session = 3
    return fake


def shows(app: QApplication, menu: CompanionMenu, text: str) -> None:
    def seen() -> bool:
        app.processEvents()
        return menu.status_action.text() == text

    wait_until(seen)


def test_menu_managed(
    app: QApplication,
    parent: QWidget,
    tmp_path: Path,
    serve: Callable[..., FakeCompanion],
) -> None:
    (tmp_path / "off").touch()
    service = CompanionService(tmp_path, linked_fake(serve).address, TOKEN)
    menu = CompanionMenu(parent, lambda: False, service)
    menu.refresh()
    shows(app, menu, "Companion: turned off")
    assert not menu.restart_action.isEnabled()
    assert menu.power_action.text() == "Turn on"
    assert menu.log_action.isVisible()


def test_menu_not_managed(
    app: QApplication,
    parent: QWidget,
    tmp_path: Path,
    serve: Callable[..., FakeCompanion],
) -> None:
    service = CompanionService(tmp_path / "missing", linked_fake(serve).address, TOKEN)
    menu = CompanionMenu(parent, lambda: False, service)
    menu.refresh()
    shows(app, menu, "Companion: running (DCS linked, mission 3)")
    assert not menu.restart_action.isVisible()
    assert not menu.power_action.isVisible()
    assert not menu.log_action.isVisible()


def test_turn_off_asks_while_following(
    app: QApplication,
    parent: QWidget,
    tmp_path: Path,
    serve: Callable[..., FakeCompanion],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CompanionService(tmp_path, linked_fake(serve).address, TOKEN)
    menu = CompanionMenu(parent, lambda: True, service)
    menu.refresh()
    shows(app, menu, "Companion: running (DCS linked, mission 3)")
    assert menu.power_action.text() == "Turn off"

    answers = [QMessageBox.StandardButton.No]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: answers[0])
    menu.power_action.trigger()
    app.processEvents()
    assert not (tmp_path / "off").exists()

    answers[0] = QMessageBox.StandardButton.Yes
    menu.power_action.trigger()
    wait_until(lambda: (tmp_path / "off").exists())


def test_restart_with_no_companion_says_so(
    app: QApplication,
    parent: QWidget,
    tmp_path: Path,
    serve: Callable[..., FakeCompanion],
) -> None:
    # The folder is here and the port answers, but no companion runs under
    # the pid file (none written): Restart signals nothing and says so.
    service = CompanionService(tmp_path, linked_fake(serve).address, TOKEN)
    menu = CompanionMenu(parent, lambda: False, service)
    menu.refresh()
    shows(app, menu, "Companion: running (DCS linked, mission 3)")
    assert menu.restart_action.isEnabled()
    menu.restart_action.trigger()
    shows(app, menu, "Companion: no companion process to restart")
