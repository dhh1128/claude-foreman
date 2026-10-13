"""Keep pane-inspection instructions from losing prompt-line provenance."""

from pathlib import Path
import re


SKILL = Path(__file__).resolve().parents[1] / "skills" / "zamu" / "SKILL.md"


def test_pm_pane_inspection_preserves_prompt_style_and_provenance():
    text = SKILL.read_text()
    captures = re.findall(r"tmux capture-pane[^\n`]*", text)
    assert captures, "the PM needs a concrete pane-inspection command"
    assert all(re.search(r"(?:^|\s)-e(?:\s|$)", command) for command in captures)
    assert "SGR 1" in text
    assert "provenance is unknown" in text
    assert "Daniel's words" in text
