"""Extract a worker's last words from one visible tmux pane capture."""

from __future__ import annotations

import re

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
BOX = "│┃║╭╮╰╯┌┐┘─━═╶╴╺╸┬┴┼"
CLAUDE_STATUS = re.compile(r"^✻\s+\S+\s+for\s+\S+")
CODEX_FOOTER = re.compile(r"^[\w.-]+(?:\s+[\w.-]+)?\s+·\s+(?:~|/)[^·]+\s+·")
CODEX_WORKING = re.compile(r"^•\s+Working\s+\([^)]*\)")
CODEX_DONE = re.compile(r"^Worked for \d.*\s+•\s+\d{1,2}:\d{2}$")
CONTEXT_LEFT = re.compile(r"^\d+% context left$")
INPUT_PREVIEW = 60


def _chrome(line: str) -> bool:
    return (not line or line.startswith(("❯", "›", "⏵", "? for shortcuts",
                                         "shift+tab to cycle", "Tip:", "+ Show details",
                                         "• Ran ", "• Messages to be submitted", "↳", "└",
                                         "+ ")) or
            bool(CLAUDE_STATUS.match(line) or CODEX_FOOTER.match(line) or
                 CODEX_WORKING.match(line) or CODEX_DONE.match(line) or
                 CONTEXT_LEFT.match(line)))


def _preview_input(parts: list[str]) -> str | None:
    text = " ".join(" ".join(parts).split())
    if not text or text.startswith(("Ask Codex to do anything", "Ask Claude", "Try ")):
        return None
    if len(text) > INPUT_PREVIEW:
        text = text[:INPUT_PREVIEW].rsplit(" ", 1)[0] + "…"
    escaped = text.replace('"', r'\"')
    return f'(unsent input: "{escaped}")'


def last_visible_line(capture: str) -> str | None:
    """Skip Claude and Codex borders, prompts, and status chrome."""
    raw_lines = [ANSI.sub("", raw) for raw in capture.splitlines()]
    lines = [raw.strip().strip(BOX + " ").strip() for raw in raw_lines]
    indented = [raw.startswith(("  ", "\t")) for raw in raw_lines]

    # Codex displays queued messages and tool results as blocks with wrapped continuation lines.
    nonworker_block = False
    skip: set[int] = set()
    for i, line in enumerate(lines):
        if line.startswith(("• Messages to be submitted", "└")):
            nonworker_block = True
            skip.add(i)
        elif nonworker_block and line.startswith(("• ", "❯", "›")):
            nonworker_block = False
        elif nonworker_block and (not line or indented[i] or line.startswith(("↳", "+"))):
            skip.add(i)
        else:
            nonworker_block = False

    # A bottom input box can wrap over several indented lines. An output line below a historical
    # prompt is not part of that box, so reject a candidate as soon as such a line appears.
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].startswith(("❯", "›")):
            continue
        parts = [lines[i][1:].strip()]
        for j in range(i + 1, len(lines)):
            if _chrome(lines[j]) or j in skip:
                continue
            if not indented[j] or lines[j].startswith(("•", "↳", "└", "+")):
                break
            parts.append(lines[j])
        else:
            return _preview_input(parts) or _last_worker_line(lines[:i], skip)

    return _last_worker_line(lines, skip)


def _last_worker_line(lines: list[str], skip: set[int]) -> str | None:
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if i not in skip and not _chrome(line):
            return " ".join(line.removeprefix("• ").split())
    return None
