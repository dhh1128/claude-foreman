"""Leftover processes and quiet panes, as the board reports them."""

from __future__ import annotations

import datetime as dt

from foreman import engine, render, store
from foreman.config import Config
from foreman.plan import parse
from foreman.procs import Proc

T0 = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.timezone.utc)
WT = "/r/.worktrees/a"


def _setup(tmp_path):
    plan = parse('[items.a]\ntitle="A"\nkind="build"\nrepo="/r"\noracle="manual"\n'
                 '[items.b]\ntitle="B"\nkind="build"\nrepo="/r"\noracle="manual"\n',
                 tmp_path / "plan.toml")
    w0 = engine.World(now=lambda: T0)
    engine.adopt(plan, tmp_path, w0, "a", "s1", "%5", WT, "")
    return plan


def _world(t, ps, activity=None):
    return engine.World(now=lambda: t, scan=lambda: ps,
                        pane_activity=lambda p: activity, pane_has_session=lambda p, s: True)


def test_an_orphaned_suite_is_reported_with_its_idleness_and_owner(tmp_path):
    plan = _setup(tmp_path)
    ps = [Proc(901, 1, WT, 50, 7, "python -m pytest -n 8", ""),
          Proc(902, 77, WT, 3, 8, "claude", "bash")]
    res = engine.pulse(plan, tmp_path, Config(), _world(T0, ps))
    assert res["changes"] == ["leftover pid 901 (a): orphaned"]
    later = T0 + dt.timedelta(minutes=45)
    res = engine.pulse(plan, tmp_path, Config(), _world(later, ps))
    assert res["changes"] == []  # already reported; not repeated every wake
    doc = store.read(tmp_path)
    (line,) = [a for a in render.summary(plan, doc, Config(), later)["attention"] if "leftover" in a]
    assert "orphaned, idle 45m" in line or ("idle 45m" in line and "orphaned" in line)
    assert "pids 901" in line and "tell its worker" in line


def test_a_scan_that_cannot_look_keeps_the_last_findings(tmp_path):
    plan = _setup(tmp_path)
    engine.pulse(plan, tmp_path, Config(), _world(T0, [Proc(901, 1, WT, 5, 7, "x", "")]))
    engine.pulse(plan, tmp_path, Config(), _world(T0, None))
    assert len(store.read(tmp_path)["leftovers"]) == 1


def test_a_quiet_pane_is_flagged_unless_the_worker_said_it_is_waiting(tmp_path):
    plan = _setup(tmp_path)
    quiet_since = (T0 - dt.timedelta(minutes=50)).timestamp()
    engine.pulse(plan, tmp_path, Config(), _world(T0, [], quiet_since))
    att = render.summary(plan, store.read(tmp_path), Config(), T0)["attention"]
    assert any(a.startswith("a: pane quiet 50m") for a in att)
    engine.report(plan, tmp_path, _world(T0, []), "a", "waiting", "approve node @x")
    att = render.summary(plan, store.read(tmp_path), Config(), T0)["attention"]
    assert not any("pane quiet" in a for a in att)


def test_reap_kills_only_what_no_live_worker_can_own(tmp_path):
    plan = _setup(tmp_path)
    ps = [Proc(901, 1, WT, 5, 7, "pytest", ""),
          Proc(903, 1, "/x/.worktrees/old (deleted)", 5, 9, "pytest", "")]
    killed = []
    lines = engine.reap(plan, tmp_path, Config(), _world(T0, ps), go=False)
    assert [l.split()[0] for l in lines] == ["leave", "would"]
    lines = engine.reap(plan, tmp_path, Config(), _world(T0, ps), go=True,
                        kill=lambda p: killed.append(p.pid) or f"{p.pid} terminated")
    assert killed == [903]
