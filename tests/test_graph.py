from __future__ import annotations

import datetime as dt
from pathlib import Path

from zamu import graph
from zamu.plan import parse

BODY = """
[plan]
due = 2026-11-17

[items.ext]
title = "Upstream release"
kind = "external"
oracle = "manual"
estimate = "5d"

[items.a]
title = "A"
kind = "build"
repo = "/r/one"
oracle = "manual"
depends_on = ["ext"]
estimate = "3d"

[items.b]
title = "B"
kind = "build"
repo = "/r/one"
oracle = "manual"
depends_on = ["a"]
estimate = "4d"
due = 2026-11-01

[items.c]
title = "C"
kind = "build"
repo = "/r/one"
oracle = "manual"

[items.d]
title = "D"
kind = "decision"
question = "Which posture do we take for the demo issuer, strict or permissive?"
"""


def _p():
    return parse(BODY, Path("/p/plan.toml"))


def _doc(**states):
    return {"items": {k: {"state": v} for k, v in states.items()}}


def test_states_and_ready_set_follow_dependencies():
    p = _p()
    s = graph.states(p, _doc())
    assert s == {"ext": "ready", "a": "blocked", "b": "blocked", "c": "ready", "d": "ready"}
    assert graph.launchable(p, s) == ["c"]
    s = graph.states(p, _doc(ext="done"))
    assert s["a"] == "ready" and s["b"] == "blocked"
    assert graph.launchable(p, s) == ["a", "c"]


def test_recorded_active_states_win_over_computed():
    s = graph.states(_p(), _doc(ext="done", a="running", c="review"))
    assert s["a"] == "running" and s["c"] == "review"
    assert graph.launchable(_p(), s) == []


def test_abandoned_strands_dependents_rather_than_unblocking_them():
    p = _p()
    s = graph.states(p, _doc(ext="abandoned"))
    assert s["a"] == "blocked"
    assert graph.stranded(p, s) == {"a": ["ext"], "b": ["ext"]}


def test_risk_uses_the_longest_unfinished_chain():
    p = _p()
    s = graph.states(p, _doc())
    # b needs ext(5) + a(3) + b(4) = 12 days, due 2026-11-01.
    r = {x.item: x for x in graph.risks(p, s, dt.date(2026, 10, 25))}
    assert r["b"].needs_days == 12 and r["b"].over_days == 5
    assert r["b"].chain == ("ext", "a", "b")
    assert "a" not in r  # 8 days to 2026-11-17 is fine
    # Once ext is done, b needs 7 days: 2026-10-25 + 7 = 2026-11-01, on time.
    s = graph.states(p, _doc(ext="done"))
    assert "b" not in {x.item for x in graph.risks(p, s, dt.date(2026, 10, 25))}


def test_overdue_is_flagged_even_without_estimates():
    p = _p()
    s = graph.states(p, _doc())
    r = {x.item: x for x in graph.risks(p, s, dt.date(2026, 11, 18))}
    assert r["c"].overdue and r["d"].overdue


def test_inverted_dues():
    body = BODY.replace('[items.ext]\n', '[items.ext]\ndue = 2026-11-10\n')
    p = parse(body, Path("/p/plan.toml"))
    assert ("b", "a") not in graph.inverted_dues(p)
    assert ("a", "ext") not in graph.inverted_dues(p)
    body2 = BODY.replace('[items.a]\n', '[items.a]\ndue = 2026-11-10\n')
    assert graph.inverted_dues(parse(body2, Path("/p/plan.toml"))) == [("b", "a")]


def test_contention_waits_until_two_items_are_in_review():
    p = _p()
    assert graph.contention(p, graph.states(p, _doc(ext="done"))) == {}
    assert graph.contention(p, graph.states(p, _doc(ext="done", a="review", c="running"))) == {}
    s = graph.states(p, _doc(ext="done", a="review", c="review"))
    assert graph.contention(p, s) == {"/r/one": ["a", "c"]}


def test_an_item_that_touches_a_second_repo_contends_there_too():
    body = BODY.replace('[items.c]\n', '[items.c]\ntouches = ["/r/two"]\n') + '''
[items.e]
title = "E"
kind = "build"
repo = "/r/two"
oracle = "manual"
'''
    p = parse(body, Path("/p/plan.toml"))
    assert p.items["c"].repos == ("/r/one", "/r/two")
    s = graph.states(p, _doc(ext="done", c="review", e="review"))
    assert graph.contention(p, s) == {"/r/two": ["c", "e"]}
