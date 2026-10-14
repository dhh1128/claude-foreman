"""Extract a worker's last words from one visible tmux pane capture."""

from __future__ import annotations

import re

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
BOX = "│┃║╭╮╰╯┌┐└┘─━═╶╴╺╸┬┴┼+"
CLAUDE_STATUS = re.compile(r"^✻\s+\S+\s+for\s+\S+")
CODEX_FOOTER = re.compile(r"^[\w.-]+(?:\s+[\w.-]+)?\s+·\s+(?:~|/)[^·]+\s+·")
CODEX_WORKING = re.compile(r"^•\s+Working\s+\([^)]*\)")
CODEX_DONE = re.compile(r"^Worked for \d.*\s+•\s+\d{1,2}:\d{2}$")
CONTEXT_LEFT = re.compile(r"^\d+% context left$")


def _chrome(line: str) -> bool:
    return (not line or line.startswith(("❯", "›", "⏵", "? for shortcuts",
                                         "shift+tab to cycle", "Tip:", "+ Show details",
                                         "• Ran ")) or
            bool(CLAUDE_STATUS.match(line) or CODEX_FOOTER.match(line) or
                 CODEX_WORKING.match(line) or CODEX_DONE.match(line) or
                 CONTEXT_LEFT.match(line)))


def last_visible_line(capture: str) -> str | None:
    """Skip Claude and Codex borders, prompts, and status chrome."""
    lines = [ANSI.sub("", raw).strip().strip(BOX + " ").strip()
             for raw in capture.splitlines()]
    # A bottom input box has only chrome below it. A historical prompt can have worker output
    # below it, so it must not truncate that output.
    prompt = next((i for i in range(len(lines) - 1, -1, -1)
                   if lines[i].startswith(("❯", "›")) and
                   all(_chrome(line) for line in lines[i + 1:])), None)
    if prompt is not None:
        lines = lines[:prompt]
    for line in reversed(lines):
        if not _chrome(line):
            return " ".join(line.removeprefix("• ").split())
    return None
