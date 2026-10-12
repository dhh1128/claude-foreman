from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from zamu.plan import PlanError, parse

P = Path("/plans/x/plan.toml")


def _plan(body: str):
    return parse(body, P)


def test_minimal_build_item_gets_defaults():
    p = _plan("""
[items.a]
title = "Do a"
kind = "build"
repo = "/r/a"
oracle = "file:/tmp/a"
brief = "briefs/a.md"
""")
    a = p.items["a"]
    assert a.topic == "a"
    assert a.brief == "/plans/x/briefs/a.md"
    assert a.oracle == ("file:/tmp/a",)
    assert p.name == "x"


def test_dangling_dependency_is_refused_and_named():
    with pytest.raises(PlanError, match="'b'.*not in the plan"):
        _plan("""
[items.a]
title = "A"
kind = "external"
oracle = "manual"
depends_on = ["b"]
""")


def test_cycle_is_refused_with_its_path():
    with pytest.raises(PlanError, match="cycle: a -> b -> c -> a"):
        _plan("""
[items.a]
title = "A"
kind = "external"
oracle = "manual"
depends_on = ["b"]
[items.b]
title = "B"
kind = "external"
oracle = "manual"
depends_on = ["c"]
[items.c]
title = "C"
kind = "external"
oracle = "manual"
depends_on = ["a"]
""")


def test_self_dependency_is_refused():
    with pytest.raises(PlanError, match="itself"):
        _plan('[items.a]\ntitle="A"\nkind="external"\noracle="manual"\ndepends_on="a"\n')


@pytest.mark.parametrize("body,msg", [
    ('[items.a]\ntitle="A"\nkind="chore"\noracle="manual"\n', "kind 'chore'"),
    ('[items.a]\nkind="external"\noracle="manual"\n', "needs a title"),
    ('[items.a]\ntitle="A"\nkind="build"\noracle="manual"\n', "needs a repo"),
    ('[items.a]\ntitle="A"\nkind="build"\nrepo="/r"\n', "needs an exit oracle"),
    ('[items.a]\ntitle="A"\nkind="external"\noracle="manual"\nstate="running"\n',
     "cannot be set in the plan"),
    ('[items.a]\ntitle="A"\nkind="external"\noracle="manual"\ncolour="red"\n',
     "unknown keys"),
    ('[items.A]\ntitle="A"\nkind="external"\noracle="manual"\n', "ids are lowercase"),
    ('[items.a]\ntitle="A"\nkind="external"\noracle="manual"\ndue="next week"\n',
     "not a date"),
    ('[items.a]\ntitle="A"\nkind="external"\noracle="manual"\nestimate="soon"\n',
     "not a duration"),
    ('[plan]\nname="x"\n', "no \\[items"),
    ('items = 3\n[foo]\n', "no \\[items"),
    ('this is not toml', "not valid TOML"),
])
def test_bad_plans_are_refused_with_a_fix(body, msg):
    with pytest.raises(PlanError, match=msg):
        _plan(body)


def test_two_items_cannot_share_a_worktree():
    with pytest.raises(PlanError, match="both want the worktree"):
        _plan("""
[items.a]
title = "A"
kind = "build"
repo = "/r"
topic = "t"
oracle = "manual"
[items.b]
title = "B"
kind = "build"
repo = "/r/"
topic = "t"
oracle = "manual"
""")


def test_decision_needs_no_oracle_and_plan_due_is_inherited():
    p = _plan("""
[plan]
due = 2026-11-17
[items.d]
title = "Trust posture"
kind = "decision"
question = "Which trust posture does the issuer take for the demo?"
[items.e]
title = "E"
kind = "external"
oracle = "manual"
due = "2026-11-01"
estimate = "2w"
""")
    assert p.items["d"].oracle == ()
    assert p.items["d"].due == dt.date(2026, 11, 17)
    assert p.items["e"].due == dt.date(2026, 11, 1)
    assert p.items["e"].estimate_days == 14


def test_dependents():
    p = _plan("""
[items.a]
title="A"
kind="external"
oracle="manual"
[items.b]
title="B"
kind="external"
oracle="manual"
depends_on=["a"]
""")
    assert p.dependents("a") == ["b"]


def test_malformed_oracle_is_refused_at_load():
    with pytest.raises(PlanError, match="item 'a'.*pr:owner/repo#N"):
        _plan('[items.a]\ntitle="A"\nkind="external"\noracle="merged upstream"\n')


def test_pins_rules_owns_engine_and_rests_on():
    p = _plan("""
[plan]
freeze = 2026-10-16
owns = ["docs/status.md"]
[pins.core]
cmd = "git rev-parse HEAD"
repo = "/r/core"
[pins.lib]
cmd = "cat lib.pin"
freeze = 2026-10-01
[items.a]
title = "A"
kind = "build"
repo = "/r/a"
oracle = "manual"
engine = "codex"
rests_on = ["core", "lib"]
""")
    assert p.items["a"].engine == "codex" and p.items["a"].rests_on == ("core", "lib")
    assert p.pins["core"].freeze == dt.date(2026, 10, 16)  # inherited from [plan]
    assert p.pins["lib"].freeze == dt.date(2026, 10, 1)
    assert p.pins["core"].repo == "/r/core"
    assert p.rules == "/plans/x/rules.md"
    assert p.owns == ("/plans/x/docs/status.md",)


def test_resting_on_an_undeclared_pin_is_refused():
    with pytest.raises(PlanError, match="rests on pin 'core'"):
        _plan('[items.a]\ntitle="A"\nkind="external"\noracle="manual"\nrests_on="core"\n')


def test_a_pin_needs_a_command():
    with pytest.raises(PlanError, match="needs cmd"):
        _plan('[pins.core]\nrepo="/r"\n[items.a]\ntitle="A"\nkind="external"\noracle="manual"\n')
