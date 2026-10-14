"""An idle worker gives back its slot while its conversation and worktree survive."""

from __future__ import annotations

import datetime as dt
import json
import os

import pytest

from zamu import cli, engine, graph, launch, render, store
from zamu.config import Config, ModelSpec
from zamu.coord import Coord
from zamu.plan import parse
from zamu.procs import Proc

T0 = dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc)


def setup_worker(tmp_path, *, waiting=True):
    repo = tmp_path / "repo"
    tree = repo / ".worktrees" / "a"
    tree.mkdir(parents=True)
    plan = parse(f'[items.a]\ntitle="A"\nkind="build"\nrepo="{repo}"\noracle="manual"\n',
                 tmp_path / "plan.toml")
    with store.transaction(tmp_path) as doc:
        doc["items"]["a"] = {"state": "running", "engine": "claude", "session": "sid",
                             "token": "sid", "pane": "%1", "ours": True, "wid": "a",
                             "worker": "live", "worktree": str(tree),
                             "last_report": engine._iso(T0), "last_line": "await approval"}
        if waiting:
            doc["items"]["a"]["waiting"] = "approve Q-1234"
        doc.setdefault("workers", {})["a"] = {"items": ["a"], "engine": "claude", "session": "sid",
                                "token": "sid", "pane": "%1", "ours": True,
                                "worktree": str(tree), "worker": "live"}
    return plan, tree


def world(t, calls, *, cpu=10, child=False, lock=False):
    ps = [Proc(1, 0, "/r", 1, 100, "sh"),
          Proc(2, 1, "/r", cpu, 101, "claude --resume sid")]
    if child:
        ps.append(Proc(3, 2, "/r", 1, 102, "gh pr checks"))

    def run(argv):
        calls.append(list(argv))
        if argv[:2] == ["git", "-C"]:
            return 0, "?? draft.txt\n", ""
        return 0, "", ""

    return engine.World(now=lambda: t, scan=lambda: ps, pane_pid=lambda _p: 1,
                        pane_has_session=lambda _p, _s: True,
                        pane_activity=lambda _p: T0.timestamp(),
                        merge_locked=lambda _r: lock, run=run)


def test_waiting_worker_parks_and_preserves_dirty_files(tmp_path):
    plan, tree = setup_worker(tmp_path)
    (tree / "draft.txt").write_text("unfinished")
    calls = []
    engine.park_due(plan, tmp_path, Config(park_after_minutes=30), world(T0, calls))
    changes = engine.park_due(plan, tmp_path, Config(park_after_minutes=30),
                              world(T0 + dt.timedelta(minutes=31), calls))
    rec = store.read(tmp_path)["items"]["a"]
    assert changes == ["a parked: approve Q-1234"]
    assert graph.states(plan, store.read(tmp_path))["a"] == "parked"
    assert rec["parked"]["dirty"] == ["draft.txt"]
    assert rec["parked"]["asks"] == ["Q-1234"]
    assert any(c[:2] == ["tmux", "kill-window"] for c in calls)
    assert engine.slots(plan, store.read(tmp_path), Config())[0] == 0
    assert "a (parked" in render.board(plan, store.read(tmp_path), Config(), T0)


def test_busy_tree_and_merge_lock_prevent_parking(tmp_path):
    plan, _ = setup_worker(tmp_path)
    calls = []
    engine.park_due(plan, tmp_path, Config(park_after_minutes=30), world(T0, calls))
    for kw in ({"cpu": 11}, {"child": True}, {"lock": True}):
        engine.park_due(plan, tmp_path, Config(park_after_minutes=30),
                        world(T0 + dt.timedelta(minutes=31), calls, **kw))
        assert graph.states(plan, store.read(tmp_path))["a"] == "running"
    assert not any(c[:2] == ["tmux", "kill-window"] for c in calls)


def test_resume_after_ask_dissolves_reuses_session_and_takes_slot(tmp_path):
    plan, _ = setup_worker(tmp_path)
    calls = []
    class Queue:
        open = {"Q-1234"}

        def open_asks(self, _repo):
            return self.open

    queue = Queue()
    w = world(T0, calls)
    w.coord = queue
    engine.park_due(plan, tmp_path, Config(), w)
    w = world(T0 + dt.timedelta(minutes=31), calls)
    w.coord = queue
    engine.park_due(plan, tmp_path, Config(), w)
    queue.open = set()
    w = world(T0 + dt.timedelta(minutes=32), calls)
    w.coord = queue
    assert engine.ready_to_resume(plan, tmp_path, Config(), w) == ["a"]
    ln, = engine.prepare(plan, tmp_path, Config(), w, ["a"], parked=True)
    assert ln.session == "sid" and ln.resume
    assert "Q-1234" in ln.steps[-1].shell()
    engine.commit_launch(plan, tmp_path, Config(), w, ln)
    rec = store.read(tmp_path)["items"]["a"]
    assert rec["state"] == "running" and rec["session"] == "sid"
    assert engine.slots(plan, store.read(tmp_path), Config())[0] == 1


def test_parked_resume_keeps_the_launched_model_after_plan_tier_changes(tmp_path):
    plan, tree = setup_worker(tmp_path)
    plan = parse(f'[items.a]\ntitle="A"\nkind="build"\nrepo="{tree.parent.parent}"\n'
                 'oracle="manual"\ntier="deep"\n', tmp_path / 'plan.toml')
    with store.transaction(tmp_path) as doc:
        doc['items']['a'].update(state='parked', tier='light', model='sonnet',
                                 reasoning_effort='', parked={'session': 'sid'})
    cfg = Config(models={'claude': {'deep': ModelSpec('fable')}})
    ln, = engine.prepare(plan, tmp_path, cfg, world(T0, []), ['a'], parked=True,
                         unblocked='approval arrived')
    assert 'tier light, model sonnet' in render.show(plan, store.read(tmp_path), 'a', [], cfg)
    assert (ln.tier, ln.model) == ('light', 'sonnet')
    assert ('--model', 'sonnet') in [ln.steps[-1].argv[i:i + 2]
                                    for i in range(len(ln.steps[-1].argv))]


def test_open_ask_prevents_automatic_resume(tmp_path):
    plan, _ = setup_worker(tmp_path)
    calls = []
    engine.park_due(plan, tmp_path, Config(), world(T0, calls))
    engine.park_due(plan, tmp_path, Config(), world(T0 + dt.timedelta(minutes=31), calls))

    class Queue:
        def open_asks(self, _repo):
            return {"Q-1234"}

    w = world(T0 + dt.timedelta(minutes=32), calls)
    w.coord = Queue()
    assert engine.ready_to_resume(plan, tmp_path, Config(), w) == []


def test_unfiled_ask_needs_pm_reason(tmp_path):
    plan, _ = setup_worker(tmp_path)
    calls = []
    engine.park_due(plan, tmp_path, Config(), world(T0, calls))
    engine.park_due(plan, tmp_path, Config(), world(T0 + dt.timedelta(minutes=31), calls))

    class Queue:
        def open_asks(self, _repo):
            return set()

    w = world(T0 + dt.timedelta(minutes=32), calls)
    w.coord = Queue()
    assert engine.ready_to_resume(plan, tmp_path, Config(), w) == []
    with pytest.raises(engine.EngineError, match="not observed open"):
        engine.prepare(plan, tmp_path, Config(), w, ["a"], parked=True)
    ln, = engine.prepare(plan, tmp_path, Config(), w, ["a"], parked=True,
                         unblocked="approval arrived by another channel")
    assert "approval arrived by another channel" in ln.steps[-1].shell()


def test_quiet_worker_parks_but_no_resume_path_does_not(tmp_path):
    plan, _ = setup_worker(tmp_path, waiting=False)
    calls = []
    engine.park_due(plan, tmp_path, Config(), world(T0, calls))
    engine.park_due(plan, tmp_path, Config(), world(T0 + dt.timedelta(minutes=31), calls))
    assert graph.states(plan, store.read(tmp_path))["a"] == "parked"

    plan, _ = setup_worker(tmp_path / "other", waiting=False)
    with store.transaction(tmp_path / "other") as doc:
        doc["items"]["a"]["engine"] = "unknown"
    engine.park_due(plan, tmp_path / "other", Config(), world(T0, []))
    engine.park_due(plan, tmp_path / "other", Config(), world(T0 + dt.timedelta(minutes=31), []))
    assert graph.states(plan, store.read(tmp_path / "other"))["a"] == "running"
    assert "idle but cannot park" in render.board(plan, store.read(tmp_path / "other"),
                                                    Config(), T0 + dt.timedelta(minutes=31))


def test_unowned_waiting_worker_says_why_it_cannot_park(tmp_path):
    plan, _ = setup_worker(tmp_path)
    with store.transaction(tmp_path) as doc:
        doc["items"]["a"]["ours"] = False
    engine.park_due(plan, tmp_path, Config(), world(T0 + dt.timedelta(minutes=31), []))
    board = render.board(plan, store.read(tmp_path), Config(), T0 + dt.timedelta(minutes=31))
    assert "window ownership and resume path are not proven" in board
    assert graph.states(plan, store.read(tmp_path))["a"] == "running"


def test_watcher_owns_the_one_pm_message_for_a_park(tmp_path):
    plan, _ = setup_worker(tmp_path)
    calls = []

    class Queue:
        def __init__(self):
            self.msgs = []

        def open_asks(self, _repo):
            return {"Q-1234"}

        def live_sessions(self, _repo):
            return {"sid"}

        def msg(self, repo, target, line):
            self.msgs.append(line)
            return True, ""

    queue = Queue()
    with store.transaction(tmp_path) as doc:
        doc["pm"] = {"repo": str(tmp_path), "target": "pm"}
    w = world(T0, calls)
    w.coord = queue
    engine.pulse(plan, tmp_path, Config(), w, notify_parks=False)
    w = world(T0 + dt.timedelta(minutes=31), calls)
    w.coord = queue
    changes = engine.pulse(plan, tmp_path, Config(), w, notify_parks=False)["changes"]
    assert changes == ["a parked: approve Q-1234"]
    assert queue.msgs == []


def test_codex_rollout_id_is_required_and_resume_command_uses_it(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    sessions = home / "sessions" / "2026" / "10" / "14"
    sessions.mkdir(parents=True)
    tree = str(tmp_path / "tree")
    launched = engine._iso(T0)
    meta = {"type": "session_meta", "payload": {"id": "real-codex-id", "cwd": tree,
            "timestamp": engine._iso(T0 + dt.timedelta(seconds=1))}}
    rollout = sessions / "rollout.jsonl"
    rollout.write_text(json.dumps(meta) + "\n")
    os.utime(rollout, (T0.timestamp() + 2, T0.timestamp() + 2))
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert cli._codex_session(tree, launched) == "real-codex-id"
    plan = parse(f'[items.a]\ntitle="A"\nkind="build"\nrepo="{tmp_path}"\noracle="manual"\n',
                 tmp_path / "plan.toml")
    cmd = launch.plan_launch(plan, plan.items["a"], tmux_session="", claude=("codex",),
                             engine="codex", default_branch="main", worktree_exists=True,
                             branch_exists=True, worktree=tree, session="real-codex-id",
                             changed="Q-1234 closed", parked=True).steps[-1].argv
    assert ("resume", "real-codex-id") == cmd[cmd.index("resume"):cmd.index("resume") + 2]
    assert "Since you were parked, Q-1234 closed." in cmd[-1]
    assert "You were relaunched as the worker" in launch.worker_prompt(
        plan, plan.items["a"], tree, True)


def test_coord_distinguishes_unreadable_asks_and_merge_lock(tmp_path):
    def run(argv):
        if argv[1:3] == ["ask", "list"]:
            return 0, "Open asks, in the order they have to happen.\n  [Q] Q-1234  text\n", ""
        return 0, "merge: held by CI merger", ""
    coord = Coord(run=run, binary="session-coord")
    assert coord.open_asks(str(tmp_path)) == {"Q-1234"}
    assert coord.merge_locked(str(tmp_path)) is True
