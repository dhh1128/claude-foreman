"""Long external waits: who an item waits on, how long, a nag, a decision held until a wait
exceeds its budget, and an ask filed when the wait is on the user."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from foreman import engine, render, store
from foreman.config import Config
from foreman.coord import Coord
from foreman.plan import PlanError, parse

T0 = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.timezone.utc)

PLAN = """
[items.prep]
title = "Prepare the parser fault sink branch and draft its PR text"
kind = "build"
repo = "/r/keripy"
oracle = "manual"

[items.posted]
title = "Post the parser fault sink PR to upstream keripy"
kind = "external"
depends_on = ["prep"]
waiting_on = "user"
question = "Open it from the prepared branch with the drafted text."
brief = "/tmp/draft-1580.md"
oracle = "manual"

[items.merged]
title = "Upstream merges the parser fault sink PR"
kind = "external"
depends_on = ["posted"]
waiting_on = "Sam Smith"
nag_after = "14d"
oracle = "manual"

[items.fallback]
title = "Publish with a documented fork pin instead of waiting"
kind = "decision"
question = "Upstream has not merged the fault sink after three weeks: publish imbu on the fork pin now and converge later, or keep waiting?"
ask_when = "merged waits > 21d"
"""


class Asks(Coord):
    def __init__(self):
        self.filed = []

    def live_sessions(self, repo):
        return None

    def ask_add(self, repo, tag, kind, text, depends_on=()):
        self.filed.append((tag, kind, text))
        return True, f"filed {tag}"


def _plan(tmp_path):
    return parse(PLAN, tmp_path / "plan.toml")


def _pulse(plan, d, coord, t):
    engine.pulse(plan, d, Config(), engine.World(coord=coord, now=lambda: t))
    return render.summary(plan, store.read(d), Config(), t)


def test_an_external_wait_on_the_user_files_one_A_ask(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_USER", "Daniel")
    plan, c = _plan(tmp_path), Asks()
    engine.mark(plan, tmp_path, engine.World(now=lambda: T0), "prep", "done", "")
    _pulse(plan, tmp_path, c, T0)
    _pulse(plan, tmp_path, c, T0 + dt.timedelta(hours=1))
    (tag, kind, text), = [f for f in c.filed if f[1] == "A"]
    assert tag.startswith("A-")
    assert "please do this yourself: Post the parser fault sink PR" in text
    assert "/tmp/draft-1580.md" in text and "Upstream merges" in text


def test_waiting_on_a_name_matches_the_user_by_name(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_USER", "Daniel")
    plan = parse(PLAN.replace('waiting_on = "user"', 'waiting_on = "Daniel"'),
                 tmp_path / "plan.toml")
    c = Asks()
    engine.mark(plan, tmp_path, engine.World(now=lambda: T0), "prep", "done", "")
    _pulse(plan, tmp_path, c, T0)
    assert [k for _, k, _ in c.filed] == ["A"]


def test_a_long_wait_on_the_maintainer_is_aged_nagged_and_triggers_the_held_decision(tmp_path):
    plan, c = _plan(tmp_path), Asks()
    w = engine.World(now=lambda: T0)
    engine.mark(plan, tmp_path, w, "prep", "done", "")
    engine.mark(plan, tmp_path, w, "posted", "done", "")
    s = _pulse(plan, tmp_path, c, T0)
    assert "merged (external, on Sam Smith)" in s["waiting"]
    assert "fallback (decision, held until merged waits > 21d)" in s["waiting"]

    s = _pulse(plan, tmp_path, c, T0 + dt.timedelta(days=15))
    assert "merged (external, on Sam Smith 15d)" in s["waiting"]
    assert "merged: waiting on Sam Smith 15d (nag after 14d)" in s["attention"]
    assert not [f for f in c.filed if f[1] == "Q"]

    s = _pulse(plan, tmp_path, c, T0 + dt.timedelta(days=22))
    (q,) = [f for f in c.filed if f[1] == "Q"]
    assert "publish imbu on the fork pin now" in q[2]
    assert any(x.startswith("fallback (decision, asked Q-") for x in s["waiting"])


def test_a_held_decision_is_not_asked_once_the_wait_it_watches_is_over(tmp_path):
    plan, c = _plan(tmp_path), Asks()
    w = engine.World(now=lambda: T0)
    for i in ("prep", "posted", "merged"):
        engine.mark(plan, tmp_path, w, i, "done", "")
    s = _pulse(plan, tmp_path, c, T0 + dt.timedelta(days=60))
    assert not [f for f in c.filed if f[1] == "Q"]
    assert "fallback (decision, held until merged finished; not needed)" in s["waiting"]


@pytest.mark.parametrize("body,msg", [
    ('[items.a]\ntitle="A"\nkind="build"\nrepo="/r"\noracle="manual"\nask_when="2026-11-01"\n',
     "only to decision items"),
    ('[items.a]\ntitle="A"\nkind="decision"\nask_when="b waits > 3d"\n', "'b', which is not"),
    ('[items.a]\ntitle="A"\nkind="decision"\nask_when="eventually"\n', "is not '<item> waits"),
])
def test_bad_ask_when_is_refused(body, msg):
    with pytest.raises(PlanError, match=msg):
        parse(body, Path("/p/plan.toml"))


def test_a_dated_ask_when(tmp_path):
    plan = parse('[items.d]\ntitle="D"\nkind="decision"\nquestion="Which posture do we take for the release, strict or lax?"\n'
                 'ask_when="2026-11-01"\n', tmp_path / "plan.toml")
    c = Asks()
    _pulse(plan, tmp_path, c, T0)
    assert c.filed == []
    _pulse(plan, tmp_path, c, dt.datetime(2026, 11, 1, 9, tzinfo=dt.timezone.utc))
    assert [k for _, k, _ in c.filed] == ["Q"]
