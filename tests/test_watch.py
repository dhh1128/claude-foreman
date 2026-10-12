"""The watcher: autolaunch into free slots, and wake the PM only with news."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from zamu import config, engine, store, watch
from zamu.coord import Coord

from fakes import make_repo

T0 = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.timezone.utc)


class FakeCoord(Coord):
    def __init__(self):
        self.msgs = []
        self.binary = "session-coord"

    def live_sessions(self, repo):
        return None

    def ask_add(self, repo, tag, kind, text, depends_on=()):
        return True, f"filed {tag}"

    def msg(self, repo, target, text):
        self.msgs.append((repo, target, text))
        return True, ""


def _setup(tmp_path, autolaunch=True, items=3):
    for n in range(items):
        make_repo(tmp_path, f"r{n}")
    d = config.plans_dir() / "p"
    d.mkdir(parents=True)
    body = f"[plan]\nautolaunch = {'true' if autolaunch else 'false'}\n"
    for n in range(items):
        body += (f'[items.i{n}]\ntitle="Item {n}"\nkind="build"\n'
                 f'repo="{tmp_path / "repos" / f"r{n}"}"\noracle="manual"\n')
    (d / "plan.toml").write_text(body)
    return d


def _world(coord, ran):
    def run(argv):
        ran.append(list(argv))
        if argv[:2] == ["tmux", "new-window"]:
            return 0, f"%{200 + len(ran)}\n", ""
        if argv[:2] == ["git", "-C"] and "symbolic-ref" in argv:
            return 0, "origin/main\n", ""
        if "rev-parse" in argv:
            return 1, "", ""
        return 0, "", ""
    return lambda: engine.World(coord=coord, run=run, now=lambda: T0,
                                pane_has_session=lambda p, s: True)


def test_autolaunch_fills_free_slots_and_tells_the_pm_once(tmp_path, monkeypatch):
    monkeypatch.setenv("ZAMU_MAX_WORKERS", "2")
    d = _setup(tmp_path)
    c, ran = FakeCoord(), []
    engine.attach(d, engine.World(now=lambda: T0), str(tmp_path), "pm-x", "%1", "bakobo")
    t = watch.tick(d, config.load(), _world(c, ran), oracles_due=True)
    assert [l.split()[0] for l in t.launched] == ["i0", "i1"]
    tmux = [a for a in ran if a[:2] == ["tmux", "new-window"]]
    assert len(tmux) == 2 and all(a[a.index("-t") + 1] == "bakobo:" for a in tmux)
    ((repo, target, text),) = c.msgs
    assert target == "pm-x" and "launched i0" in text and "zamu board --plan p" in text

    # Nothing new: no launch (cap reached), no message.
    t = watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert not t.news and len(c.msgs) == 1

    # A slot frees: the next tick fills it, without anyone being asked.
    engine.mark(watch.load(d / "plan.toml", "p"), d, engine.World(now=lambda: T0), "i0", "done", "")
    t = watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert [l.split()[0] for l in t.launched] == ["i2"]


def test_without_autolaunch_nothing_is_launched(tmp_path):
    d = _setup(tmp_path, autolaunch=False)
    c, ran = FakeCoord(), []
    t = watch.tick(d, config.load(), _world(c, ran), oracles_due=True)
    assert t.launched == [] and not [a for a in ran if a[:2] == ["tmux", "new-window"]]


def test_attention_is_sent_once_and_again_only_if_it_clears_and_returns(tmp_path):
    d = _setup(tmp_path, autolaunch=False)
    plan = watch.load(d / "plan.toml", "p")
    w0 = engine.World(now=lambda: T0)
    engine.attach(d, w0, str(tmp_path), "pm-x")
    engine.adopt(plan, d, w0, "i0", "s1", "", "", "")
    c, ran = FakeCoord(), []
    engine.report(plan, d, w0, "i0", "waiting", "approve decision node @abc")
    c.msgs.clear()
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert len(c.msgs) == 1 and "i0: waiting on the user" in c.msgs[0][2]
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert len(c.msgs) == 1
    engine.report(plan, d, w0, "i0", "running", "approved")
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    engine.report(plan, d, w0, "i0", "waiting", "approve the second node")
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert "approve the second node" in c.msgs[-1][2]


def test_numbers_in_attention_do_not_make_it_new():
    assert watch.key("g1: waiting on Sam 15d (nag after 14d)") == \
        watch.key("g1: waiting on Sam 16d (nag after 14d)")


def test_exit_on_news_prints_and_exits_without_messaging(tmp_path, monkeypatch):
    monkeypatch.setenv("ZAMU_MAX_WORKERS", "1")
    d = _setup(tmp_path, items=1)
    c, ran, out = FakeCoord(), [], []
    engine.attach(d, engine.World(now=lambda: T0), str(tmp_path), "pm-x")
    rc = watch.run("p", 0.01, 0.01, once=False, exit_on_news=True,
                   base_world=_world(c, ran), out=out.append)
    assert rc == 0 and "launched i0" in out[0] and c.msgs == []


def test_a_second_watcher_refuses(tmp_path):
    fd = watch._single_instance("watch-all")
    try:
        try:
            watch._single_instance("watch-all")
            raise AssertionError("second watcher started")
        except watch.AlreadyRunning:
            pass
    finally:
        os.close(fd)


def test_the_service_unit_runs_the_stable_shim(tmp_path):
    shim, shim_body, unit, body = watch.service_files(Path("/plug/zamu/1"), {"PATH": "/usr/bin"})
    assert f"ExecStart={shim} watch" in body and 'Environment="PATH=/usr/bin"' in body
    assert '"zamu@zamu"' in shim_body and "d=/plug/zamu/1" in shim_body


def test_admission_refusal_holds_launches_and_explains(tmp_path, monkeypatch):
    d = _setup(tmp_path)
    Path(os.environ["ZAMU_CONFIG"]).write_text('admission = ["headroom", "check"]\n')
    c, ran = FakeCoord(), []

    def busy_world():
        w = _world(c, ran)()
        inner = w.run
        w.run = lambda argv: ((75, "not now: cpu: tasks waited 70%", "")
                              if argv[0] == "headroom" else inner(argv))
        return w
    t = watch.tick(d, config.load(), busy_world, oracles_due=True)
    assert t.launched == []
    assert any("launches wait, the box is busy: cpu: tasks waited 70%" in a for a in t.attention)


def test_admission_admits_one_launch_per_tick(tmp_path, monkeypatch):
    d = _setup(tmp_path)
    Path(os.environ["ZAMU_CONFIG"]).write_text('admission = "headroom check"\n')
    c, ran = FakeCoord(), []

    def ok_world():
        w = _world(c, ran)()
        inner = w.run
        w.run = lambda argv: (0, "admit", "") if argv[0] == "headroom" else inner(argv)
        return w
    assert len(watch.tick(d, config.load(), ok_world, oracles_due=True).launched) == 1
    assert len(watch.tick(d, config.load(), ok_world, oracles_due=False).launched) == 1


def test_an_admission_command_that_cannot_run_falls_back_to_the_count(tmp_path):
    d = _setup(tmp_path)
    Path(os.environ["ZAMU_CONFIG"]).write_text('admission = ["no-such-headroom"]\n')
    c, ran = FakeCoord(), []

    def broken():
        w = _world(c, ran)()
        inner = w.run

        def run(argv):
            if argv[0] == "no-such-headroom":
                raise FileNotFoundError(argv[0])
            return inner(argv)
        w.run = run
        return w
    t = watch.tick(d, config.load(), broken, oracles_due=True)
    assert len(t.launched) == 1
    assert any("admission unknown, count cap only" in a for a in t.attention)
