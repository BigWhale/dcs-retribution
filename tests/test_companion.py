import logging

import pytest

from game.radio.companion import CompanionPair, plan_companion
from game.radio.radios import MHz, kHz


def test_companion_plan(caplog: pytest.LogCaptureFixture) -> None:
    busy = [
        MHz(121, 500),
        MHz(243),
        MHz(125),
        MHz(125, 10),  # 125.025 is within 25 kHz of it
        MHz(260),
        MHz(289, 975),
        MHz(136, 960),  # 136.975 and 136.950 are within 25 kHz of it
        MHz(284, 875),
    ]
    plan = plan_companion(busy, airfields=[12, 3], awacs=["Overlord", "Magic"])

    # ATIS from the bottom, by airbase id; White Force and AWACS from the top.
    assert plan.atis == {
        3: CompanionPair(vhf=MHz(125, 50), uhf=MHz(260, 25)),
        12: CompanionPair(vhf=MHz(125, 75), uhf=MHz(260, 50)),
    }
    assert plan.white_force == MHz(289, 950)
    assert plan.awacs == {
        "Overlord": CompanionPair(vhf=MHz(136, 925), uhf=MHz(284, 850)),
        "Magic": CompanionPair(vhf=MHz(136, 900), uhf=MHz(284, 825)),
    }

    picked = plan.frequencies()
    assert len(picked) == 9
    for f in picked:
        assert f.hertz % 25_000 == 0
        assert (
            125_000_000 <= f.hertz <= 136_975_000
            or 260_000_000 <= f.hertz <= 289_975_000
        )
        assert all(abs(f.hertz - b.hertz) >= 25_000 for b in busy)
        others = [o for o in picked if o is not f]
        assert all(abs(f.hertz - o.hertz) >= 25_000 for o in others)

    # A full VHF range leaves the band out, with a warning.
    every_vhf = [kHz(125_000 + 25 * i) for i in range(480)]
    with caplog.at_level(logging.WARNING):
        full = plan_companion(every_vhf, airfields=[3], awacs=["Overlord"])
    assert full.atis == {3: CompanionPair(vhf=None, uhf=MHz(260))}
    assert full.awacs == {"Overlord": CompanionPair(vhf=None, uhf=MHz(284, 875))}
    assert "no free" in caplog.text
