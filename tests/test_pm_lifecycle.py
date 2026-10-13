"""A PM starts from its plan brief and can be replaced after its session dies."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from zamu import config, engine, store, watch
from zamu.coord import Coord

from fakes import World


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def test_init_writes_standing_pm_brief(w):
    w.zamu("init", "roadmap")
    brief = config.plans_dir() / "roadmap" / "pm.md"
    text = brief.read_text()
    assert "# PM brief" in text
    assert "zamu attach" in text and "zamu pulse" in text
    assert "zamu-dev" not in text


def test_pm_start_dry_run_and_go_use_attached_repo_session_and_fresh_id(w):
    w.zamu("init", "roadmap")
    d = config.plans_dir() / "roadmap"
    (d / "plan.toml").write_text('[plan]\nname="roadmap"\n[items.a]\ntitle="A"\nkind="decision"\n')
    repo = w.tmp / "project"
    repo.mkdir()
    with store.transaction(d) as doc:
        doc["pm"] = {"repo": str(repo), "target": "old-pm", "tmux_session": "project"}
    dry = w.zamu("pm", "start", "roadmap").stdout
    assert "tmux new-window" in dry and "-t project:" in dry
    assert str(d / "pm.md") in dry
    assert "zamu attach --plan roadmap" in dry and "zamu pulse --plan roadmap" in dry
    assert "dry run" in dry
    assert w.calls("tmux") == []
    out = w.zamu("pm", "start", "roadmap", "--go").stdout
    assert "started PM" in out
    (call,) = w.calls("tmux")
    assert call[call.index("-t") + 1] == "project:"
    assert call[call.index("-c") + 1] == str(repo)
    assert "--session-id" in call
    assert store.read(d)["pm"]["target"] == call[call.index("--session-id") + 1]


def test_pm_restart_requires_confirmed_dead_and_override_session(w):
    w.zamu("init", "roadmap")
    d = config.plans_dir() / "roadmap"
    (d / "plan.toml").write_text('[plan]\nname="roadmap"\n[items.a]\ntitle="A"\nkind="decision"\n')
    with store.transaction(d) as doc:
        doc["pm"] = {"repo": str(w.tmp), "target": "old-pm", "tmux_session": "old",
                     "status": "dead"}
    out = w.zamu("pm", "restart", "roadmap", "--tmux-session", "new").stdout
    assert "-t new:" in out and "--resume" not in out
    w.zamu("pm", "restart", "roadmap", "--go")
    assert store.read(d)["pm"]["status"] == "starting"
    assert w.calls("tmux")[0][w.calls("tmux")[0].index("-t") + 1] == "old:"
    assert w.zamu("pm", "restart", "roadmap", check=False).returncode == 2


def test_first_pm_can_start_from_current_repo_with_tmux_override(w):
    w.zamu("init", "roadmap")
    d = config.plans_dir() / "roadmap"
    (d / "plan.toml").write_text('[plan]\nname="roadmap"\n[items.a]\ntitle="A"\nkind="decision"\n')
    assert "-t project:" in w.zamu(
        "pm", "start", "roadmap", "--tmux-session", "project").stdout
    w.zamu("pm", "start", "roadmap", "--tmux-session", "project", "--go")
    assert store.read(d)["pm"]["repo"] == str(w.tmp)


def test_pm_uses_configured_claude_command(w):
    w.zamu("init", "roadmap")
    d = config.plans_dir() / "roadmap"
    (d / "plan.toml").write_text('[plan]\nname="roadmap"\n[items.a]\ntitle="A"\nkind="decision"\n')
    conf = w.tmp / "config.toml"
    conf.write_text('[engines]\nclaude=["claude", "--permission-mode", "acceptEdits"]\n')
    out = w.zamu("pm", "start", "roadmap", "--tmux-session", "project",
                 env={"ZAMU_CONFIG": str(conf)}).stdout
    assert "claude --permission-mode acceptEdits --session-id" in out


def test_pm_target_liveness_distinguishes_unreadable_from_absent():
    rows = '[{"title":"pm-name","pane":"%8","session_id":"sid"}]'
    c = Coord(run=lambda argv: (0, rows, ""))
    assert c.live_target("/repo", "pm-name") is True
    assert c.live_target("/repo", "%8") is True
    assert c.live_target("/repo", "sid") is True
    assert c.live_target("/repo", "absent") is False
    assert Coord(run=lambda argv: (1, "", "failed")).live_target("/repo", "sid") is None
    assert Coord(run=lambda argv: (0, '[null]', "")).live_target("/repo", "sid") is None
    assert Coord(run=lambda argv: (0, '[{}]', "")).live_target("/repo", "sid") is None


class PMCoord(Coord):
    def __init__(self):
        self.rows = []
        self.asks = []
        self.msgs = []

    def live_target(self, repo, target):
        return None if self.rows is None else any(
            target in (r.get("title"), r.get("pane"), r.get("session_id")) for r in self.rows)

    def live_sessions(self, repo):
        return set()

    def ask_add(self, repo, tag, kind, text, depends_on=()):
        self.asks.append((repo, tag, kind, text))
        return True, f"filed {tag}"

    def msg(self, repo, target, text):
        self.msgs.append((repo, target, text))
        return True, ""


def test_dead_pm_ask_once_then_again_only_after_seen_alive(tmp_path, monkeypatch):
    from test_watch import T0, _setup, _world
    monkeypatch.setenv("ZAMU_MAX_WORKERS", "1")
    d = _setup(tmp_path, autolaunch=True, items=1)
    c, ran = PMCoord(), []
    engine.attach(d, engine.World(now=lambda: T0), str(tmp_path), "pm-x", "%1", "project")
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert len(c.asks) == 1 and c.asks[0][2] == "A"
    assert "zamu pm restart p" in c.asks[0][3]
    assert c.msgs == []  # a dead PM cannot receive watcher news
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert len(c.asks) == 1
    c.rows = None
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert len(c.asks) == 1
    c.rows = [{"title": "pm-x", "session_id": "sid"}]
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    c.rows = []
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert len(c.asks) == 2 and c.asks[0][1] != c.asks[1][1]


def test_dead_pm_without_work_or_attention_does_not_ask(tmp_path):
    from test_watch import T0, _setup, _world
    d = _setup(tmp_path, autolaunch=False, items=1)
    c, ran = PMCoord(), []
    engine.attach(d, engine.World(now=lambda: T0), str(tmp_path), "pm-x")
    engine.mark(watch.load(d / "plan.toml", "p"), d,
                engine.World(now=lambda: T0), "i0", "done", "finished")
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert c.asks == []


def test_dead_pm_with_attention_but_no_active_worker_asks(tmp_path):
    from test_watch import T0, _world
    d = config.plans_dir() / "p"
    d.mkdir(parents=True)
    (d / "plan.toml").write_text('[plan]\n[items.a]\ntitle="Due"\nkind="external"\n'
                                 'oracle="manual"\ndue=2026-10-11\n')
    c, ran = PMCoord(), []
    engine.attach(d, engine.World(now=lambda: T0), str(tmp_path), "pm-x")
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert len(c.asks) == 1 and "zamu pm restart p" in c.asks[0][3]


def test_pm_launched_without_attach_is_detected_after_grace(w):
    from test_watch import T0
    w.zamu("init", "roadmap")
    d = config.plans_dir() / "roadmap"
    (d / "plan.toml").write_text(
        f'[plan]\n[items.a]\ntitle="A"\nkind="build"\nrepo="{w.tmp}"\noracle="manual"\n')
    w.zamu("pm", "start", "roadmap", "--tmux-session", "project", "--go")
    sid = store.read(d)["pm"]["target"]
    plan = watch.load(d / "plan.toml", "roadmap")
    engine.adopt(plan, d, engine.World(now=lambda: T0), "a", "worker-sid", "", "", "")
    c = PMCoord()
    later = engine._parse(store.read(d)["pm"]["since"]) + dt.timedelta(minutes=10)
    world = lambda: engine.World(coord=c, now=lambda: later,
                                 cwd=lambda: str(w.tmp))
    watch.tick(d, config.load(), world, oracles_due=False)
    pm = store.read(d)["pm"]
    assert pm["repo"] == str(w.tmp) and pm["target"] == sid
    assert pm["status"] == "dead" and len(c.asks) == 1


def test_attach_during_pm_liveness_check_preserves_new_target(tmp_path):
    from test_watch import T0, _setup
    d = _setup(tmp_path, autolaunch=False, items=1)
    engine.attach(d, engine.World(now=lambda: T0), str(tmp_path), "old-pm")
    c = PMCoord()

    def replace_pm(repo, target):
        engine.attach(d, engine.World(now=lambda: T0 + dt.timedelta(minutes=1)),
                      str(tmp_path), "new-pm")
        return False

    c.live_target = replace_pm
    plan = watch.load(d / "plan.toml", "p")
    changes = engine.check_pm(plan, d, config.load(),
                              engine.World(coord=c, now=lambda: T0 + dt.timedelta(minutes=2)))
    pm = store.read(d)["pm"]
    assert changes == [] and pm["target"] == "new-pm" and pm["status"] == "attached"
    assert not pm.get("dead_ask") and c.asks == []


def test_start_refuses_present_or_unreadable_old_pm_board(w):
    w.zamu("init", "roadmap")
    d = config.plans_dir() / "roadmap"
    (d / "plan.toml").write_text('[plan]\n[items.a]\ntitle="A"\nkind="decision"\n')
    with store.transaction(d) as doc:
        doc["pm"] = {"repo": str(w.tmp), "target": "old-pm", "tmux_session": "project"}
    (w.fake / "board.json").write_text(json.dumps([
        {"title": "old-pm", "pane": "%9", "session_id": "sid"}]))
    r = w.zamu("pm", "start", "roadmap", "--go", check=False)
    assert r.returncode == 2 and "still alive" in r.stderr and w.calls("tmux") == []
    (w.fake / "board.json").write_text("not json")
    r = w.zamu("pm", "start", "roadmap", "--go", check=False)
    assert r.returncode == 2 and "could not check" in r.stderr and w.calls("tmux") == []
