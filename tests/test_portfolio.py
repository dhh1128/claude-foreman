"""One compact, read-only view over plans and their live workers."""

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

from zamu import config, portfolio, procs
from zamu.config import Config
from zamu.engine import World
from zamu.procs import Proc

NOW = dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc)


def plan(root, name, items, *, pins="", state=None, autolaunch=True):
    d = root / name
    d.mkdir(parents=True)
    (d / "plan.toml").write_text(f"[plan]\nautolaunch = {str(autolaunch).lower()}\n" + items + pins)
    (d / "state.json").write_text(json.dumps(state or {"items": {}, "pm": {}}))
    return d


def test_portfolio_ranks_asks_and_finds_cross_plan_conflicts(tmp_path):
    root = config.plans_dir()
    root.mkdir(parents=True)
    repo = str(tmp_path / "repo")
    plan(root, "alpha", f'''[items.decide]\nkind="decision"\ntitle="Choose release"\nquestion="Ship now or wait?"\n[items.build]\nkind="build"\ntitle="Build"\nrepo="{repo}"\noracle="manual"\ndepends_on=["decide"]\n[items.finish]\nkind="external"\ntitle="Finish"\noracle="manual"\ndepends_on=["build"]\n[pins.base]\ncmd="echo v1"\nrepo="{repo}"\n''', state={"items": {"decide": {"ask": "Q-ONE"}}, "pm": {"status": "live"}, "watch": {"last_tick": NOW.isoformat()}})
    plan(root, "beta", f'''[items.work]\nkind="build"\ntitle="Change repo"\nrepo="{repo}"\noracle="manual"\n''', state={"items": {"work": {"state": "running", "wid": "w", "worker": "live", "engine": "codex", "session": "s1", "worktree": repo + "/.worktrees/work"}}, "pm": {"status": "dead"}})
    plan(root, "gamma", f'''[items.work]\nkind="build"\ntitle="Change repo again"\nrepo="{repo}"\noracle="manual"\n''', state={"items": {"work": {"state": "review", "wid": "w2", "worker": "live", "engine": "claude", "session": "s2", "worktree": repo + "/.worktrees/work2", "waiting": "Approve merge"}}, "pm": {}})
    fake = World(now=lambda: NOW, scan=lambda: [], run=lambda argv: (0, "active\n", ""))
    data = portfolio.snapshot(Config(max_workers=4), fake, claude_projects=tmp_path / "missing", codex_sessions=tmp_path / "missing")
    assert data["box"]["workers"] == {"claude": 1, "codex": 1}
    assert [p["name"] for p in data["plans"]][:2] == ["beta", "gamma"]
    assert data["waiting"][0]["id"] == "alpha/decide"
    assert data["waiting"][0]["unblocks"] == 2
    assert data["waiting"][1]["text"] == "Approve merge"
    assert any(c["kind"] == "repo" for c in data["conflicts"])
    assert any(c["kind"] == "pin" and c["pin"] == "alpha/base" for c in data["conflicts"])
    assert "?" in portfolio.board(data)  # missing usage remains unknown
    assert "Run1" in portfolio.board(data) and "Rev1" in portfolio.board(data)


def test_portfolio_attributes_child_resources_and_session_usage(tmp_path):
    root = config.plans_dir()
    root.mkdir(parents=True)
    repo = str(tmp_path / "repo")
    state = {"items": {"work": {"state": "running", "wid": "w", "worker": "live", "engine": "claude", "session": "session-a", "worktree": repo + "/.worktrees/work", "launched": NOW.isoformat()}}, "pm": {}}
    plan(root, "alpha", f'[items.work]\nkind="build"\ntitle="Work"\nrepo="{repo}"\noracle="manual"\n', state=state)
    ps = [Proc(10, 1, repo + "/.worktrees/work", 1, 1, "claude --session-id session-a", rss_bytes=1000, cpu_pct=10), Proc(11, 10, "/tmp", 1, 2, "pytest", rss_bytes=2000, cpu_pct=25)]
    projects = tmp_path / "projects"
    projects.mkdir()
    old = {"timestamp": (NOW - dt.timedelta(hours=1)).isoformat(), "message": {"id": "old", "model": "claude-sonnet", "usage": {"input_tokens": 1000}}}
    current = {"timestamp": NOW.isoformat(), "message": {"id": "m1", "model": "claude-sonnet", "usage": {"input_tokens": 10, "output_tokens": 2}}}
    (projects / "session-a.jsonl").write_text(json.dumps(old) + "\n" + json.dumps(current) + "\n")
    data = portfolio.snapshot(Config(max_workers=4), World(now=lambda: NOW, scan=lambda: ps, run=lambda argv: (0, "active", "")), claude_projects=projects, codex_sessions=tmp_path / "none")
    p = data["plans"][0]
    assert p["rss_bytes"] == 3000
    assert p["cpu_pct"] == 35
    assert p["spent"] == {"claude": 60}  # sonnet factor 3 * (10 input + 5 * 2 output)
    weighted = portfolio.snapshot(Config(max_workers=4), World(now=lambda: NOW, scan=lambda: ps, run=lambda argv: (0, "active", "")), claude_projects=projects, codex_sessions=tmp_path / "none", claude_factors={"sonnet": 2})
    assert weighted["plans"][0]["spent"] == {"claude": 40}


def test_portfolio_is_budgeted_and_survives_broken_other_plan(tmp_path):
    root = config.plans_dir()
    root.mkdir(parents=True)
    for n in range(4):
        items = "".join(f'[items.i{i}]\nkind="external"\ntitle="Item {i}"\noracle="manual"\n' for i in range(20))
        plan(root, f"p{n}", items)
    plan(root, "broken", '[items.i]\nkind="decision"\ntitle="Broken"\n')
    (root / "broken" / "plan.toml").write_text("bad TOML [")
    data = portfolio.snapshot(Config(), World(now=lambda: NOW, scan=lambda: [], run=lambda argv: (0, "active", "")), claude_projects=tmp_path / "none", codex_sessions=tmp_path / "none")
    board = portfolio.board(data)
    assert "broken" in board and "could not load" in board
    assert len(data["plans"]) == 4
    assert len(board) < 2000


def test_codex_session_spend_uses_latest_cumulative_count(tmp_path):
    root = config.plans_dir()
    root.mkdir(parents=True)
    repo = str(tmp_path / "repo")
    plan(root, "alpha", f'[items.work]\nkind="build"\ntitle="Work"\nrepo="{repo}"\noracle="manual"\n', state={"items": {"work": {"state": "running", "wid": "w", "worker": "live", "engine": "codex", "session": "session-b"}}, "pm": {}})
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    rows = [{"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": n}}}} for n in (10, 25)]
    (sessions / "rollout-session-b.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n")
    data = portfolio.snapshot(Config(), World(now=lambda: NOW, scan=lambda: [], run=lambda argv: (0, "active", "")), claude_projects=tmp_path / "none", codex_sessions=sessions)
    assert data["plans"][0]["spent"] == {"codex": 25}


def test_launched_codex_worker_finds_its_rollout_by_worktree_and_token(tmp_path):
    root = config.plans_dir()
    root.mkdir(parents=True)
    repo = str(tmp_path / "repo")
    wt = repo + "/.worktrees/work"
    plan(root, "alpha", f'[items.work]\nkind="build"\ntitle="Work"\nrepo="{repo}"\noracle="manual"\n', state={"items": {"work": {"state": "running", "wid": "w", "worker": "live", "engine": "codex", "token": "launch-token", "worktree": wt, "launched": "2026-10-14T12:00:00+00:00"}}, "pm": {}})
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    old = [{"type": "session_meta", "payload": {"cwd": wt, "timestamp": "2026-10-14T11:00:00Z"}}, {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 999}}}}]
    current = [{"type": "session_meta", "payload": {"cwd": wt, "timestamp": "2026-10-14T12:00:03Z"}}, {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 42}}}}]
    (sessions / "old.jsonl").write_text("\n".join(map(json.dumps, old)) + "\n")
    (sessions / "current.jsonl").write_text("\n".join(map(json.dumps, current)) + "\n")
    ps = [Proc(10, 1, wt, 1, 1, "sh -c zamu-worker:launch-token", rss_bytes=1000), Proc(11, 10, "/tmp", 1, 2, "codex", rss_bytes=2000)]
    data = portfolio.snapshot(Config(), World(now=lambda: NOW, scan=lambda: ps, run=lambda argv: (0, "active", "")), claude_projects=tmp_path / "none", codex_sessions=sessions)
    assert data["plans"][0]["spent"] == {"codex": 42}
    assert data["plans"][0]["rss_bytes"] == 3000


def test_short_cpu_sample_uses_delta_for_existing_process(monkeypatch):
    first = [Proc(10, 1, "/w", 100, 1, "worker")]
    second = [Proc(10, 1, "/w", 120, 1, "worker", rss_bytes=4096)]
    samples = iter((first, second))
    times = iter((10.0, 10.2))
    monkeypatch.setattr(procs, "scan", lambda: next(samples))
    monkeypatch.setattr(procs.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(procs.time, "sleep", lambda interval: None)
    monkeypatch.setattr(procs.os, "sysconf", lambda name: 100)
    (got,) = procs.sample_cpu()
    assert got.cpu_pct == 100
    assert got.rss_bytes == 4096


def test_adopted_worker_child_inherits_its_ancestors_worktree():
    worker = {"id": "adopted", "engine": "codex", "session": "", "token": "", "worktree": "/repo/.worktrees/adopted", "launched": ""}
    ps = [Proc(10, 1, worker["worktree"], 1, 1, "codex", rss_bytes=1000),
          Proc(11, 10, "/tmp", 1, 2, "pytest", rss_bytes=3000)]
    assert portfolio._resources([("plan", worker)], ps)[("plan", "adopted")][0] == 4000


def test_user_waits_remain_visible_before_ask_is_filed(tmp_path):
    root = config.plans_dir()
    root.mkdir(parents=True)
    plan(root, "alpha", '[items.decide]\nkind="decision"\ntitle="Choose"\nquestion="Ship now?"\n[items.upload]\nkind="external"\ntitle="Upload file"\noracle="manual"\nwaiting_on="user"\n')
    data = portfolio.snapshot(Config(), World(now=lambda: NOW, scan=lambda: [], run=lambda argv: (0, "active", "")), claude_projects=tmp_path / "none", codex_sessions=tmp_path / "none")
    assert {w["id"] for w in data["waiting"]} == {"alpha/decide", "alpha/upload"}
    assert all(w["ask"] is None for w in data["waiting"])


def test_budget_ask_counts_the_ready_items_it_holds_and_their_dependents(tmp_path):
    root = config.plans_dir()
    root.mkdir(parents=True)
    repo = str(tmp_path / "repo")
    plan(root, "alpha", f'[items.build]\nkind="build"\ntitle="Build"\nrepo="{repo}"\noracle="manual"\n[items.ship]\nkind="external"\ntitle="Ship"\noracle="manual"\ndepends_on=["build"]\n', state={"items": {}, "pm": {}, "budget_episode": {"ask": "Q-BUDGET", "waiting": ["build"], "line": "Choose capacity"}})
    data = portfolio.snapshot(Config(), World(now=lambda: NOW, scan=lambda: [], run=lambda argv: (0, "active", "")), claude_projects=tmp_path / "none", codex_sessions=tmp_path / "none")
    assert data["waiting"][0]["unblocks"] == 2


def test_mixed_engine_spend_keeps_known_amount_when_other_log_is_missing(tmp_path):
    root = config.plans_dir()
    root.mkdir(parents=True)
    repo = str(tmp_path / "repo")
    items = "".join(f'[items.{iid}]\nkind="build"\ntitle="{iid}"\nrepo="{repo}"\noracle="manual"\n' for iid in ("a", "b"))
    state = {"items": {"a": {"state": "running", "wid": "a", "worker": "live", "engine": "claude", "session": "missing"}, "b": {"state": "running", "wid": "b", "worker": "live", "engine": "codex", "session": "known"}}, "pm": {}}
    plan(root, "alpha", items, state=state)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    row = {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 25}}}}
    (sessions / "known.jsonl").write_text(json.dumps(row) + "\n")
    data = portfolio.snapshot(Config(), World(now=lambda: NOW, scan=lambda: [], run=lambda argv: (0, "active", "")), claude_projects=tmp_path / "missing", codex_sessions=sessions)
    assert data["plans"][0]["spent"] == {"claude": None, "codex": 25}
    assert "codex ~25tok" in portfolio.board(data)


def test_pm_status_requires_a_live_check_this_snapshot(tmp_path):
    root = config.plans_dir()
    root.mkdir(parents=True)
    d = plan(root, "alpha", '[items.decide]\nkind="decision"\ntitle="Decide"\n', state={"items": {}, "pm": {"status": "live"}})
    world = World(now=lambda: NOW, scan=lambda: [], run=lambda argv: (0, "active", ""))
    world.coord = SimpleNamespace(live_target=lambda repo, target: None)
    data = portfolio.snapshot(Config(), world, claude_projects=tmp_path / "none", codex_sessions=tmp_path / "none")
    assert data["plans"][0]["pm"] == "?"
    (d / "state.json").write_text(json.dumps({"items": {}, "pm": {"status": "live", "repo": "/r", "target": "sid"}}))
    assert portfolio.snapshot(Config(), world, claude_projects=tmp_path / "none", codex_sessions=tmp_path / "none")["plans"][0]["pm"] == "?"


def test_dead_pm_ask_ranks_by_unfinished_plan_items(tmp_path):
    root = config.plans_dir()
    root.mkdir(parents=True)
    items = '[items.done]\nkind="decision"\ntitle="Done"\nstate="done"\n[items.next]\nkind="decision"\ntitle="Next"\n[items.last]\nkind="decision"\ntitle="Last"\ndepends_on=["next"]\n'
    plan(root, "alpha", items, state={"items": {}, "pm": {"dead_ask": "A-PM", "status": "dead"}})
    data = portfolio.snapshot(Config(), World(now=lambda: NOW, scan=lambda: [], run=lambda argv: (0, "active", "")), claude_projects=tmp_path / "none", codex_sessions=tmp_path / "none")
    pm_wait = next(w for w in data["waiting"] if w["id"] == "alpha/pm")
    assert pm_wait["unblocks"] == 2


def test_partial_claude_spend_keeps_measured_amount_and_marks_gap(tmp_path):
    root = config.plans_dir()
    root.mkdir(parents=True)
    repo = str(tmp_path / "repo")
    items = "".join(f'[items.{iid}]\nkind="build"\ntitle="{iid}"\nrepo="{repo}"\noracle="manual"\n' for iid in ("a", "b"))
    state = {"items": {"a": {"state": "running", "wid": "a", "worker": "live", "engine": "claude", "session": "known"}, "b": {"state": "running", "wid": "b", "worker": "live", "engine": "claude", "session": "missing"}}, "pm": {}}
    plan(root, "alpha", items, state=state)
    projects = tmp_path / "projects"
    projects.mkdir()
    row = {"timestamp": NOW.isoformat(), "message": {"id": "m", "model": "claude-sonnet", "usage": {"input_tokens": 10}}}
    (projects / "known.jsonl").write_text(json.dumps(row) + "\n")
    data = portfolio.snapshot(Config(), World(now=lambda: NOW, scan=lambda: [], run=lambda argv: (0, "active", "")), claude_projects=projects, codex_sessions=tmp_path / "none", claude_factors={})
    assert data["plans"][0]["spent"] == {"claude": 30}
    assert data["plans"][0]["spent_unknown"] == {"claude": 1}
    assert "claude ~30wtok+?" in portfolio.board(data)


def test_same_worker_id_in_two_plans_keeps_process_ownership_separate():
    a = {"id": "shared", "engine": "claude", "session": "", "token": "", "worktree": "/repo/.worktrees/a", "launched": ""}
    b = {**a, "worktree": "/repo/.worktrees/b"}
    ps = [Proc(10, 1, a["worktree"], 1, 1, "sh zamu-worker:shared", rss_bytes=1000),
          Proc(11, 10, "/tmp", 1, 2, "pytest", rss_bytes=2000),
          Proc(20, 1, b["worktree"], 1, 3, "sh zamu-worker:shared", rss_bytes=3000),
          Proc(21, 20, "/tmp", 1, 4, "pytest", rss_bytes=4000)]
    resources = portfolio._resources([("alpha", a), ("beta", b)], ps)
    assert resources[("alpha", "shared")][0] == 3000
    assert resources[("beta", "shared")][0] == 7000
