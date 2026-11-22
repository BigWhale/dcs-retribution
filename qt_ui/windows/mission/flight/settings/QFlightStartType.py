from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
)

from game.ato.flight import Flight
from game.ato.starttype import StartType
from game.theater import OffMapSpawn
from qt_ui.models import PackageModel


class QFlightStartType(QGroupBox):
    def __init__(self, package_model: PackageModel, flight: Flight):
        super().__init__()
        self.package_model = package_model
        self.flight = flight

        self.layout = QVBoxLayout()
        self.main_row = QHBoxLayout()
        self.start_type_label = QLabel("Start type:")
        self.start_type = QComboBox()

        for start_type in StartType:
            self.start_type.addItem(start_type.value, start_type)
        self.start_type.setCurrentText(flight.start_type.value)

        if isinstance(self.flight.departure, OffMapSpawn):
            self.start_type.setEnabled(False)

        self.start_type.currentTextChanged.connect(self._on_start_type_selected)
        self.main_row.addWidget(self.start_type_label)
        self.main_row.addWidget(self.start_type)

        self.layout.addLayout(self.main_row)
        self.layout.addWidget(
            QLabel(
                "Any option other than Cold will make this flight non-targetable "
                + "by OCA/Aircraft missions. This will affect game balance."
            )
        )
        self.setLayout(self.layout)

    def on_pilot_selected(self):
        # Pilot selection detected. If this is a player flight, set start_type
        # as configured for players in the settings.
        # Otherwise, set the start_type as configured for AI.
        # https://github.com/dcs-liberation/dcs_liberation/issues/1567

        if isinstance(self.flight.departure, OffMapSpawn):
            return
        elif self.package_model.dynamic_campaign:
            self._change_dynamic_start_type()
            return
        elif self.flight.roster.player_count > 0:
            self.flight.start_type = (
                self.flight.coalition.game.settings.default_start_type_client
            )
        else:
            self.flight.start_type = (
                self.flight.coalition.game.settings.default_start_type
            )

        self.start_type.setCurrentText(self.flight.start_type.value)

        self.package_model.update_tot()

    def _on_start_type_selected(self):
        selected = self.start_type.currentData()
        if self.package_model.dynamic_campaign:
            # The package keeps its Start time; its TOT moves.
            self.package_model.change_start_type(self.flight, selected)
            return
        self.flight.start_type = selected
        self.package_model.update_tot()

    def _change_dynamic_start_type(self) -> None:
        """A dynamic campaign's pilot change: the start type for players or
        for AI, the package keeping its Start time."""
        settings = self.flight.coalition.game.settings
        if self.flight.roster.player_count > 0:
            start_type = settings.default_start_type_client
        else:
            start_type = settings.default_start_type
        self.package_model.change_start_type(self.flight, start_type)
        blocked = self.start_type.blockSignals(True)
        self.start_type.setCurrentText(start_type.value)
        self.start_type.blockSignals(blocked)
