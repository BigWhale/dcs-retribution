"""DCS Companion's subtitle file in the generated mission."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from dcs import Mission
from dcs.action import DoScript
from dcs.triggers import TriggerStart

from game.missiongenerator.luagenerator import LuaGenerator
from game.missiongenerator.missiondata import MissionData
from game.radio.companion import CompanionPair, CompanionPlan
from game.radio.radios import MHz

SUBTITLE = 'subtitle = { file = "l10n/DEFAULT/dcs-companion-silence.ogg" }'


def _game(subtitles: bool) -> Any:
    theater = SimpleNamespace(
        controlpoints=[],
        ground_objects=[],
        iads_network=SimpleNamespace(skynet_nodes=lambda game: []),
    )
    return SimpleNamespace(
        settings=SimpleNamespace(dcs_companion_subtitles=subtitles),
        theater=theater,
    )


def _plan() -> CompanionPlan[str]:
    return CompanionPlan(
        atis={3: CompanionPair(vhf=MHz(125, 50), uhf=MHz(260, 25))},
        white_force=MHz(289, 950),
        awacs={},
    )


def _generate(
    subtitles: bool, plan: Optional[CompanionPlan[Any]]
) -> tuple[list[str], str]:
    """The mission's resource file names and its data script."""
    mission = Mission()
    LuaGenerator(
        _game(subtitles), mission, MissionData(companion=plan)
    ).generate_plugin_data()
    files = [
        Path(path).name
        for lang in mission.map_resource.files.values()
        for path in lang.values()
    ]
    trigger = next(
        t
        for t in mission.triggerrules.triggers
        if isinstance(t, TriggerStart) and t.comment == "Set DCS Retribution data"
    )
    action = trigger.actions[0]
    assert isinstance(action, DoScript)
    return files, action.text.id


def test_subtitle_with_companion_and_subtitles() -> None:
    files, script = _generate(subtitles=True, plan=_plan())
    assert files == ["dcs-companion-silence.ogg"]
    assert SUBTITLE in script
    assert "version = 1" in script


def test_no_subtitle_with_subtitles_off() -> None:
    files, script = _generate(subtitles=False, plan=_plan())
    assert files == []
    assert "dcsRetribution.Companion" in script
    assert "subtitle" not in script


def test_no_subtitle_with_companion_off() -> None:
    files, script = _generate(subtitles=True, plan=None)
    assert files == []
    assert "subtitle" not in script


def test_silence_file_is_in_the_fork() -> None:
    sound = Path("resources/dcs-companion/dcs-companion-silence.ogg")
    assert sound.read_bytes()[:4] == b"OggS"
