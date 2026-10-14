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
MAX_QUOTE = 80
UNSENT_PREFIX = '(unsent input: "'
UNSENT_SUFFIX = '")'


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
    escaped = text.replace('"', r'\"')
    content_limit = min(INPUT_PREVIEW + 1, MAX_QUOTE - len(UNSENT_PREFIX + UNSENT_SUFFIX))
    if len(escaped) > content_limit:
        hard = escaped[:content_limit - 1]
        at_word = hard.rsplit(" ", 1)[0]
        cut = hard if len(hard) - len(at_word) > len(hard) // 3 else at_word
        escaped = cut.rstrip("\\ ") + "…"
    return UNSENT_PREFIX + escaped + UNSENT_SUFFIX


def last_visible_line(capture: str) -> str | None:
    """Skip Claude and Codex borders, prompts, and status chrome."""
    raw_lines = [ANSI.sub("", raw) for raw in capture.splitlines()]
    lines = [raw.strip().strip(BOX + " ").strip() for raw in raw_lines]
    indented = [raw.startswith(("  ", "\t")) for raw in raw_lines]

    # Submitted prompts, tool calls, and queued messages can all wrap onto indented lines.
    block = ""
    skip: set[int] = set()
    prompt_continuations: set[int] = set()
    for i, line in enumerate(lines):
        if line.startswith(("• Messages to be submitted", "└")):
            block = "other"
            skip.add(i)
        elif line.startswith("• Ran "):
            block = "run"
            skip.add(i)
        elif line.startswith(("❯", "›")):
            block = "prompt"
            skip.add(i)
        elif block and (not line or indented[i] or line.startswith(("↳", "+"))):
            if block == "prompt" and indented[i] and line and not _chrome(line):
                prompt_continuations.add(i)
            skip.add(i)
        else:
            block = ""

    # A bottom input box can wrap over several indented lines. An output line below a historical
    # prompt is not part of that box, so reject a candidate as soon as such a line appears.
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].startswith(("❯", "›")):
            continue
        parts = [lines[i][1:].strip()]
        for j in range(i + 1, len(lines)):
            if lines[j].startswith(("• Ran ", "• Working ")):
                break
            if _chrome(lines[j]) or (j in skip and j not in prompt_continuations):
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
