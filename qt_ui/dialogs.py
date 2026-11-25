"""Application-wide dialog management."""

from typing import Optional

from game import Game
from game.ato.flight import Flight
from game.theater.missiontarget import MissionTarget
from .flightlauncher import flight_locked
from .models import GameModel, PackageModel
from .windows.mission.QEditFlightDialog import QEditFlightDialog
from .windows.mission.QPackageDialog import (
    QEditPackageDialog,
    QNewPackageDialog,
)


def planning_locked(game: Optional[Game]) -> bool:
    """Whether no package or flight may be created or edited: a dynamic
    campaign before Begin (Begin's own turn pass plans as usual)."""
    return game is not None and game.dynamic_campaign and game.turn == 0


class Dialog:
    """Dialog management singleton.

    Opens dialogs and keeps references to dialog windows so that their creators
    do not need to worry about the lifetime of the dialog object, and can open
    dialogs without needing to have their own reference to common data like the
    game model.
    """

    #: The game model. Is only None before initialization, as the game model
    #: itself is responsible for handling the case where no game is loaded.
    game_model: Optional[GameModel] = None

    new_package_dialog: Optional[QNewPackageDialog] = None
    edit_package_dialog: Optional[QEditPackageDialog] = None
    edit_flight_dialog: Optional[QEditFlightDialog] = None

    @classmethod
    def set_game(cls, game_model: GameModel) -> None:
        """Sets the game model."""
        cls.game_model = game_model

    @classmethod
    def open_new_package_dialog(cls, mission_target: MissionTarget, parent=None):
        """Opens the dialog to create a new package with the given target.

        Not in a dynamic campaign before Begin.
        """
        if cls._planning_locked():
            return
        cls.new_package_dialog = QNewPackageDialog(
            cls.game_model, mission_target, parent=parent
        )
        cls.new_package_dialog.show()

    @classmethod
    def open_edit_package_dialog(cls, package_model: PackageModel):
        """Opens the dialog to edit the given package.

        Not in a dynamic campaign before Begin.
        """
        if cls._planning_locked():
            return
        cls.edit_package_dialog = QEditPackageDialog(cls.game_model, package_model)
        cls.edit_package_dialog.show()

    @classmethod
    def open_edit_flight_dialog(
        cls, package_model: PackageModel, flight: Flight, parent=None
    ) -> None:
        """Opens the dialog to edit the given flight.

        Not in a dynamic campaign before Begin, nor for a dynamic campaign's
        flight in flight or on its way there.
        """
        if cls._planning_locked() or flight_locked(flight):
            return
        cls.edit_flight_dialog = QEditFlightDialog(
            cls.game_model, package_model, flight, parent=parent
        )
        cls.edit_flight_dialog.show()

    @classmethod
    def _planning_locked(cls) -> bool:
        game = cls.game_model.game if cls.game_model is not None else None
        return planning_locked(game)
