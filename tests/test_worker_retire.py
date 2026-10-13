"""Finished workers stay registered until their proof dies or the PM retires them."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from fakes import git, make_repo
from zamu import engine, render, store
from zamu.config import Config
from zamu.plan import parse

NOW = dt.datetime(2026, 10, 13, tzinfo=dt.timezone.utc)


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
    world = engine.World(now=lambda: NOW, pane_has_session=lambda pane, token: True)
    engine.worker_add(tmp_path, world, "cx", "codex", pane="%9", worktree=str(tree))
    engine.assign(plan, tmp_path, world, "a", "cx")
    engine.mark(plan, tmp_path, world, "a", "done", "merged")
    return plan, repo, tree


def test_finished_worker_with_dead_pane_is_not_assignable(tmp_path):
    plan, _, _ = setup_worker(tmp_path)
    dead = engine.World(now=lambda: NOW + dt.timedelta(hours=1),
                        pane_has_session=lambda pane, token: False)
    engine.pulse(plan, tmp_path, Config(), dead)
    doc = store.read(tmp_path)
    assert doc["workers"]["cx"]["worker"] == "dead"
    assert render.summary(plan, doc, Config(), NOW)["idle_workers"] == []
    with pytest.raises(engine.EngineError, match="dead"):
        engine.assign(plan, tmp_path, dead, "b", "cx")


def test_assign_checks_proof_even_before_another_pulse(tmp_path):
    plan, _, _ = setup_worker(tmp_path)
    dead = engine.World(now=lambda: NOW, pane_has_session=lambda pane, token: False)
    with pytest.raises(engine.EngineError, match="dead"):
        engine.assign(plan, tmp_path, dead, "b", "cx")


def test_unreadable_proof_does_not_restore_a_dead_worker(tmp_path):
    plan, _, _ = setup_worker(tmp_path)
    dead = engine.World(now=lambda: NOW, pane_has_session=lambda pane, token: False)
    engine.refresh_idle_workers(plan, tmp_path, dead)
    unknown = engine.World(now=lambda: NOW, pane_has_session=lambda pane, token: None)
    engine.refresh_idle_workers(plan, tmp_path, unknown)
    doc = store.read(tmp_path)
    assert doc["workers"]["cx"]["worker"] == "dead"
    assert render.summary(plan, doc, Config(), NOW)["idle_workers"] == []


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
    calls = []

    def run(argv):
        calls.append(list(argv))
        if argv[0] == "tmux":
            return 0, "", ""
        return engine.launch_mod.default_run(argv)

    world = engine.World(now=lambda: NOW, run=run)
    assert "retired" in engine.worker_retire(plan, tmp_path, world, "cx")
    assert not tree.exists()
    assert git("-C", str(repo), "branch", "--list", "a").strip() == "a"
    assert ["tmux", "kill-window", "-t", "%9"] in calls
    assert store.read(tmp_path)["workers"]["cx"]["worker"] == "retired"
    assert any(e["what"] == "worker-retire" for e in store.events(tmp_path))
    with pytest.raises(engine.EngineError, match="retired"):
        engine.assign(plan, tmp_path, world, "b", "cx")


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
    world = engine.World(now=lambda: NOW, pane_has_session=lambda pane, token: True)
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
