from datetime import datetime
from typing import Optional

from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QVBoxLayout

from game import Game
from game.companionlink.livestate import LiveStateContext
from game.sim import GameUpdateEvents
from game.timeofday import TimeOfDay
from game.weather.conditions import Conditions
from qt_ui import uiconstants as CONST
from qt_ui.companionlinkcontroller import CompanionLinkController
from qt_ui.simcontroller import SimController


class QTimeTurnWidget(QGroupBox):
    """
    UI Component to display current turn and time info

    In a dynamic campaign the box is the campaign time: the mission clock's
    date and time, to the minute, moved by every snapshot from the companion.
    """

    def __init__(
        self,
        sim_controller: SimController,
        companion_link: Optional[CompanionLinkController] = None,
    ) -> None:
        super(QTimeTurnWidget, self).__init__("Turn")
        self.sim_controller = sim_controller
        self.setStyleSheet(
            "padding: 0px; margin-left: 5px; margin-right: 0px; margin-top: 1ex; margin-bottom: 5px; border-right: 0px"
        )

        self.icons = {
            TimeOfDay.Dawn: CONST.ICONS["Dawn"],
            TimeOfDay.Day: CONST.ICONS["Day"],
            TimeOfDay.Dusk: CONST.ICONS["Dusk"],
            TimeOfDay.Night: CONST.ICONS["Night"],
        }

        # self.setProperty('style', 'conditions__widget--turn')
        self.layout = QHBoxLayout()
        self.setLayout(self.layout)

        self.daytime_icon = QLabel()
        self.daytime_icon.setPixmap(self.icons[TimeOfDay.Dawn])
        self.layout.addWidget(self.daytime_icon)

        self.time_column = QVBoxLayout()
        self.layout.addLayout(self.time_column)

        self.date_display = QLabel()
        self.time_column.addWidget(self.date_display)

        self.time_display = QLabel()
        self.time_column.addWidget(self.time_display)

        sim_controller.sim_update.connect(self.on_sim_update)
        if companion_link is not None:
            companion_link.snapshot_received.connect(self.on_snapshot)

    def _dynamic_game(self) -> Optional[Game]:
        """The loaded game when it is a dynamic campaign, else None."""
        game_loop = self.sim_controller.game_loop
        if game_loop is None or not game_loop.game.dynamic_campaign:
            return None
        return game_loop.game

    def on_sim_update(self, _events: GameUpdateEvents) -> None:
        time = self.sim_controller.current_time_in_sim_if_game_loaded
        if time is None:
            self.date_display.setText("")
            self.time_display.setText("")
        else:
            self.set_date_and_time(time)

    def on_snapshot(self, _snapshot: object = None) -> None:
        """Shows the mission clock's now in a dynamic campaign."""
        game = self._dynamic_game()
        if game is None:
            return
        now = LiveStateContext.clock().now()
        if now is None:
            return
        self._show_campaign_time(game, now)

    def _show_campaign_time(self, game: Game, now: datetime) -> None:
        time_of_day = game.theater.daytime_map.best_guess_time_of_day_at(now.time())
        self.daytime_icon.setPixmap(self.icons[time_of_day])
        self.set_date_and_time(now)

    def set_current_turn(self, turn: int, conditions: Conditions) -> None:
        """Sets the turn information display.

        In a dynamic campaign the box is the campaign time: the mission clock's
        now once a snapshot came, the start time before.

        :arg turn Current turn number.
        :arg conditions Current time and weather conditions.
        """
        game = self._dynamic_game()
        if game is not None:
            self.setTitle("Campaign time")
            now = LiveStateContext.clock().now()
            if now is not None:
                self._show_campaign_time(game, now)
                return
        else:
            self.setTitle(f"Turn {turn}")
        self.daytime_icon.setPixmap(self.icons[conditions.time_of_day])
        self.set_date_and_time(conditions.start_time)

    def set_date_and_time(self, time: datetime) -> None:
        self.date_display.setText(time.strftime("%d %b %Y"))
        if self._dynamic_game() is not None:
            self.time_display.setText(time.strftime("%H:%M Local"))
        else:
            self.time_display.setText(time.strftime("%H:%M:%S Local"))
