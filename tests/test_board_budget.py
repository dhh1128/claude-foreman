"""A waking PM reads the board every time, so its size is a budget, not a style choice."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from zamu import render
from zamu.config import Config
from zamu.plan import parse


def test_a_twenty_item_plan_board_stays_within_a_few_hundred_tokens():
    items = []
    for i in range(20):
        dep = f'depends_on = ["item-{i - 1:02d}"]\n' if i % 4 else ""
        items.append(f'[items.item-{i:02d}]\ntitle = "Work item number {i} with a long-ish title"\n'
                     f'kind = "build"\nrepo = "/code/acme/repo-{i % 5}"\n{dep}'
                     f'oracle = "pr:acme/repo-{i % 5}#{100 + i}"\n')
    plan = parse("\n".join(items), Path("/p/plan.toml"))
    doc = {"items": {
        "item-00": {"state": "done"},
        "item-04": {"state": "running", "session": "s4", "worker": "live",
                    "last_report": "2026-10-12T10:00:00+00:00",
                    "last_line": "tests green on the parser; starting the migration step now"},
        "item-08": {"state": "review", "session": "s8", "worker": "dead",
                    "last_report": "2026-10-12T09:00:00+00:00", "last_line": "PR open"},
    }}
    now = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.timezone.utc)
    out = render.board(plan, doc, Config(), now)
    # ~4 characters per token: 1600 characters is about 400 tokens.
    assert len(out) < 1600, out
    assert len(out.splitlines()) <= 15, out
