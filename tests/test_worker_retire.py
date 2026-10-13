"""Finished workers stay registered until their proof dies or the PM retires them."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from fakes import git, make_repo
from zamu import cli, engine, render, store
from zamu.config import Config
from zamu.plan import parse

NOW = dt.datetime(2026, 10, 13, tzinfo=dt.timezone.utc)


def fake_empty_display(tmp_path: Path, monkeypatch, panes: str = "", fail_list: bool = False):
    """tmux 3.6 can exit successfully with empty display output for a missing pane."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    tmux = bindir / "tmux"
    tmux.write_text("""#!/usr/bin/env python3
import os, sys
if sys.argv[1] == "list-panes":
    if os.environ.get("FAKE_TMUX_FAIL_LIST"):
        print("no server running", file=sys.stderr)
        sys.exit(1)
    print(os.environ.get("FAKE_TMUX_PANES", ""), end="")
elif sys.argv[1] == "display-message":
    print("")
else:
    sys.exit(2)
""")
    tmux.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_TMUX_PANES", panes)
    if fail_list:
        monkeypatch.setenv("FAKE_TMUX_FAIL_LIST", "1")


def test_empty_display_for_missing_pane_proves_it_gone(tmp_path, monkeypatch):
    fake_empty_display(tmp_path, monkeypatch, panes="%8\n%10\n")
    assert cli._pane_has_session("%9", "") is False
    assert cli._pane_has_session("%9", "worker-token") is False
    assert cli._pane_has_session("%8", "") is None
    assert cli._pane_exists("%8") is True
    assert cli._pane_has_session("%8", "worker-token") is None


def test_failed_list_panes_leaves_liveness_unknown(tmp_path, monkeypatch):
    fake_empty_display(tmp_path, monkeypatch, fail_list=True)
    assert cli._pane_exists("%9") is None
    assert cli._pane_has_session("%9", "") is None
    assert cli._pane_has_session("%9", "worker-token") is None


def test_gone_pane_is_dead_and_can_be_retired_without_kill(tmp_path, monkeypatch):
    plan, repo, tree = setup_worker(tmp_path)
    fake_empty_display(tmp_path, monkeypatch, panes="%8\n")
    world = engine.World(now=lambda: NOW, pane_exists=cli._pane_exists,
                         pane_has_session=cli._pane_has_session)
    engine.refresh_idle_workers(plan, tmp_path, world)
    assert store.read(tmp_path)["workers"]["cx"]["worker"] == "dead"
    assert "cx (codex)" not in render.summary(plan, store.read(tmp_path), Config(), NOW)["idle_workers"]
    calls = []

    def run(argv):
        calls.append(list(argv))
        return engine.launch_mod.default_run(argv)

    retire_world = engine.World(now=lambda: NOW, run=run, pane_exists=cli._pane_exists,
                                pane_has_session=cli._pane_has_session)
    assert "retired" in engine.worker_retire(plan, tmp_path, retire_world, "cx")
    assert not tree.exists()
    assert git("-C", str(repo), "branch", "--list", "a").strip() == "a"
    assert not any(c[0] == "tmux" for c in calls)
    assert store.read(tmp_path)["workers"]["cx"]["worker"] == "retired"


def test_present_hand_started_pane_is_live_but_cannot_be_killed(tmp_path, monkeypatch):
    plan, _, tree = setup_worker(tmp_path)
    fake_empty_display(tmp_path, monkeypatch, panes="%9\n")
    def run(argv):
        if argv[0] == "tmux":
            pytest.fail("retirement attempted to kill an unowned pane")
        return engine.launch_mod.default_run(argv)

    world = engine.World(now=lambda: NOW, run=run, pane_exists=cli._pane_exists,
                         pane_has_session=cli._pane_has_session)
    engine.refresh_idle_workers(plan, tmp_path, world)
    assert store.read(tmp_path)["workers"]["cx"]["worker"] == "live"
    with pytest.raises(engine.EngineError, match="cannot prove"):
        engine.worker_retire(plan, tmp_path, world, "cx")
    assert tree.exists()
    assert store.read(tmp_path)["workers"]["cx"]["worker"] != "retired"


def setup_worker(tmp_path: Path):
    repo = make_repo(tmp_path, "app")
    plan = parse(f'[items.a]\ntitle="A"\nkind="build"\nrepo="{repo}"\noracle="manual"\n'
                 f'[items.b]\ntitle="B"\nkind="build"\nrepo="{repo}"\noracle="manual"\n',
                 tmp_path / "plan.toml")
    tree = repo / ".worktrees" / "a"
    tree.parent.mkdir()
    git("-C", str(repo), "worktree", "add", "-q", "-b", "a", str(tree))
    (tree / ".zamu").mkdir()
    (tree / ".zamu" / ".gitignore").write_text("*\n")
    world = engine.World(now=lambda: NOW, pane_exists=lambda pane: True)
    engine.worker_add(tmp_path, world, "cx", "codex", pane="%9", worktree=str(tree))
    engine.assign(plan, tmp_path, world, "a", "cx")
    engine.mark(plan, tmp_path, world, "a", "done", "merged")
    return plan, repo, tree


def test_finished_worker_with_dead_pane_is_not_assignable(tmp_path):
    plan, _, _ = setup_worker(tmp_path)
    dead = engine.World(now=lambda: NOW + dt.timedelta(hours=1), pane_exists=lambda pane: False)
    engine.pulse(plan, tmp_path, Config(), dead)
    doc = store.read(tmp_path)
    assert doc["workers"]["cx"]["worker"] == "dead"
    assert render.summary(plan, doc, Config(), NOW)["idle_workers"] == []
    with pytest.raises(engine.EngineError, match="dead"):
        engine.assign(plan, tmp_path, dead, "b", "cx")


def test_assign_checks_proof_even_before_another_pulse(tmp_path):
    plan, _, _ = setup_worker(tmp_path)
    dead = engine.World(now=lambda: NOW, pane_exists=lambda pane: False)
    with pytest.raises(engine.EngineError, match="dead"):
        engine.assign(plan, tmp_path, dead, "b", "cx")


def test_unreadable_proof_does_not_restore_a_dead_worker(tmp_path):
    plan, _, _ = setup_worker(tmp_path)
    dead = engine.World(now=lambda: NOW, pane_exists=lambda pane: False)
    engine.refresh_idle_workers(plan, tmp_path, dead)
    unknown = engine.World(now=lambda: NOW, pane_exists=lambda pane: None)
    engine.refresh_idle_workers(plan, tmp_path, unknown)
    doc = store.read(tmp_path)
    assert doc["workers"]["cx"]["worker"] == "dead"
    assert render.summary(plan, doc, Config(), NOW)["idle_workers"] == []


def test_unreadable_proof_removes_a_live_worker_from_assignable_list(tmp_path):
    plan, _, _ = setup_worker(tmp_path)
    live = engine.World(now=lambda: NOW, pane_exists=lambda pane: True)
    engine.refresh_idle_workers(plan, tmp_path, live)
    assert render.summary(plan, store.read(tmp_path), Config(), NOW)["idle_workers"] == ["cx (codex)"]
    unknown = engine.World(now=lambda: NOW, pane_exists=lambda pane: None)
    engine.refresh_idle_workers(plan, tmp_path, unknown)
    doc = store.read(tmp_path)
    assert doc["workers"]["cx"]["worker"] == "unknown"
    assert render.summary(plan, doc, Config(), NOW)["idle_workers"] == []
    assert "idle workers, unknown: cx (codex)" in render.board(plan, doc, Config(), NOW)
    with pytest.raises(engine.EngineError, match="unknown"):
        engine.assign(plan, tmp_path, unknown, "b", "cx")


def test_launched_workers_token_must_remain_in_its_pane(tmp_path):
    plan, _, _ = setup_worker(tmp_path)
    with store.transaction(tmp_path) as doc:
        doc["workers"]["cx"].update({"ours": True, "token": "launch-id"})
    seen = []
    world = engine.World(now=lambda: NOW,
                         pane_has_session=lambda pane, token: seen.append(token) or False)
    engine.refresh_idle_workers(plan, tmp_path, world)
    assert seen == ["launch-id"]
    assert store.read(tmp_path)["workers"]["cx"]["worker"] == "dead"


def test_retire_closes_window_removes_clean_tree_and_keeps_branch(tmp_path):
    plan, repo, tree = setup_worker(tmp_path)
    with store.transaction(tmp_path) as doc:
        doc["workers"]["cx"].update({"ours": True, "token": "launch-id"})
    calls = []
    proofs = []

    def run(argv):
        calls.append(list(argv))
        if argv[0] == "tmux":
            return 0, "", ""
        return engine.launch_mod.default_run(argv)

    world = engine.World(now=lambda: NOW, run=run,
                         pane_has_session=lambda pane, token: proofs.append((pane, token)) or True)
    assert "retired" in engine.worker_retire(plan, tmp_path, world, "cx")
    assert not tree.exists()
    assert git("-C", str(repo), "branch", "--list", "a").strip() == "a"
    assert ["tmux", "kill-window", "-t", "%9"] in calls
    assert proofs == [("%9", "launch-id")]
    assert store.read(tmp_path)["workers"]["cx"]["worker"] == "retired"
    assert any(e["what"] == "worker-retire" for e in store.events(tmp_path))
    with pytest.raises(engine.EngineError, match="retired"):
        engine.assign(plan, tmp_path, world, "b", "cx")


def test_retire_refuses_to_kill_a_reused_pane(tmp_path):
    plan, _, tree = setup_worker(tmp_path)
    with store.transaction(tmp_path) as doc:
        doc["workers"]["cx"].update({"ours": True, "token": "launch-id"})
    calls = []

    def run(argv):
        calls.append(list(argv))
        return engine.launch_mod.default_run(argv)

    def pane_has_session(pane, token):
        return token == ""  # pane exists, but another process replaced our worker token

    world = engine.World(now=lambda: NOW, run=run, pane_exists=lambda pane: True,
                         pane_has_session=pane_has_session)
    with pytest.raises(engine.EngineError, match="cannot prove"):
        engine.worker_retire(plan, tmp_path, world, "cx")
    assert tree.exists()
    assert not any(c[0] == "tmux" for c in calls)


def test_retire_refuses_a_live_hand_started_pane_without_identity(tmp_path):
    plan, _, tree = setup_worker(tmp_path)
    world = engine.World(now=lambda: NOW, pane_exists=lambda pane: True)
    with pytest.raises(engine.EngineError, match="cannot prove"):
        engine.worker_retire(plan, tmp_path, world, "cx")
    assert tree.exists()


def test_retire_can_close_a_pane_with_its_recorded_session_id(tmp_path):
    plan, _, tree = setup_worker(tmp_path)
    with store.transaction(tmp_path) as doc:
        doc["workers"]["cx"]["session"] = "sid-1"
    calls = []

    def run(argv):
        calls.append(list(argv))
        return (0, "", "") if argv[0] == "tmux" else engine.launch_mod.default_run(argv)

    world = engine.World(now=lambda: NOW, run=run,
                         pane_has_session=lambda pane, identity: identity == "sid-1")
    engine.worker_retire(plan, tmp_path, world, "cx")
    assert not tree.exists()
    assert ["tmux", "kill-window", "-t", "%9"] in calls


def test_retire_skips_kill_when_the_recorded_pane_is_gone(tmp_path):
    plan, _, tree = setup_worker(tmp_path)
    calls = []

    def run(argv):
        calls.append(list(argv))
        return engine.launch_mod.default_run(argv)

    world = engine.World(now=lambda: NOW, run=run, pane_exists=lambda pane: False,
                         pane_has_session=lambda pane, identity: False)
    engine.worker_retire(plan, tmp_path, world, "cx")
    assert not tree.exists()
    assert not any(c[0] == "tmux" for c in calls)


def test_retire_refuses_dirty_tree_without_killing_window(tmp_path):
    plan, _, tree = setup_worker(tmp_path)
    (tree / "unsaved.txt").write_text("keep me")
    calls = []

    def run(argv):
        calls.append(list(argv))
        return engine.launch_mod.default_run(argv)

    with pytest.raises(engine.EngineError, match="dirty"):
        engine.worker_retire(plan, tmp_path, engine.World(now=lambda: NOW, run=run), "cx")
    assert tree.exists()
    assert not any(c[0] == "tmux" for c in calls)
    assert store.read(tmp_path)["workers"]["cx"].get("worker") != "retired"


def test_retire_refuses_active_worker(tmp_path):
    plan, _, _ = setup_worker(tmp_path)
    world = engine.World(now=lambda: NOW, pane_exists=lambda pane: True)
    engine.assign(plan, tmp_path, world, "b", "cx")
    with pytest.raises(engine.EngineError, match="active"):
        engine.worker_retire(plan, tmp_path, world, "cx")


def test_retire_never_removes_the_main_checkout(tmp_path):
    plan, repo, _ = setup_worker(tmp_path)
    with store.transaction(tmp_path) as doc:
        doc["workers"]["cx"]["worktree"] = str(repo)
    calls = []

    def run(argv):
        calls.append(list(argv))
        return engine.launch_mod.default_run(argv)

    with pytest.raises(engine.EngineError, match="main checkout"):
        engine.worker_retire(plan, tmp_path, engine.World(now=lambda: NOW, run=run), "cx")
    assert repo.exists()
    assert not any(c[0] == "tmux" for c in calls)
