"""Extract a worker's last words from one visible tmux pane capture."""

from __future__ import annotations

import re

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
BOX = "│┃║╭╮╰╯┌┐└┘─━═╶╴╺╸┬┴┼+"


def last_visible_line(capture: str) -> str | None:
    """Skip Claude's borders, prompt, and status chrome; None means no worker line."""
    lines = [ANSI.sub("", raw).strip().strip(BOX + " ").strip()
             for raw in capture.splitlines()]
    # Everything below the input prompt belongs to the UI, whatever wording its footer uses.
    prompt = next((i for i in range(len(lines) - 1, -1, -1)
                   if lines[i].startswith("❯")), None)
    if prompt is not None:
        lines = lines[:prompt]
    for line in reversed(lines):
        if not line or line.startswith(("❯", "⏵", "✻ Worked for")):
            continue
        if line.startswith(("? for shortcuts", "shift+tab to cycle")):
            continue
        return " ".join(line.split())
    return None
