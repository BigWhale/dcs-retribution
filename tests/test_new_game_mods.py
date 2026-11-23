"""The new game wizard starts with every mod unticked, whatever the campaign
asks for: lake's server has no mods, and a mission that needs a missing mod
fails to load."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from game.campaignloader.campaign import Campaign  # noqa: E402
from qt_ui.windows.newgame.WizardPages.QGeneratorSettings import (  # noqa: E402
    GeneratorOptions,
)

NOISY_CRICKET = Path("resources/campaigns/operation_noisy_cricket.yaml")


def test_campaign_mods_start_unticked() -> None:
    _app = QApplication.instance() or QApplication([])
    campaign = Campaign.from_file(NOISY_CRICKET)
    # The campaign asks for four mods and a full start.
    assert campaign.settings["hercules"] and campaign.settings["uh_60l"]
    assert campaign.settings["squadron_start_full"]

    page = GeneratorOptions(campaign)

    ticked = [cb for cb in page.mod_checkboxes if cb.isChecked()]
    assert page.mod_checkboxes and ticked == []
    # The campaign's other generator settings still apply.
    assert page.squadrons_start_full.isChecked()
