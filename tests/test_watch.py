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

    def live_target(self, repo, target):
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


def test_unmapped_tier_does_not_stop_another_autolaunch(tmp_path, monkeypatch):
    monkeypatch.setenv("ZAMU_MAX_WORKERS", "1")
    d = _setup(tmp_path, items=2)
    p = d / "plan.toml"
    p.write_text(p.read_text().replace('[items.i0]\n', '[items.i0]\ntier="deep"\n'))
    c, ran = FakeCoord(), []
    t = watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert [line.split()[0] for line in t.launched] == ["i1"]
    assert any("i0" in line and "no model for tier" in line for line in t.attention)


def test_without_autolaunch_nothing_is_launched(tmp_path):
    d = _setup(tmp_path, autolaunch=False)
    c, ran = FakeCoord(), []
    t = watch.tick(d, config.load(), _world(c, ran), oracles_due=True)
    assert t.launched == [] and not [a for a in ran if a[:2] == ["tmux", "new-window"]]


def test_waiting_report_reaches_pm_once_and_stays_on_board(tmp_path):
    d = _setup(tmp_path, autolaunch=False)
    plan = watch.load(d / "plan.toml", "p")
    c, ran = FakeCoord(), []
    w0 = engine.World(coord=c, now=lambda: T0, pane=lambda: "%2")
    engine.attach(d, w0, str(tmp_path), "pm-x", pane="%1")
    engine.adopt(plan, d, w0, "i0", "s1", "", "", "")
    engine.report(plan, d, w0, "i0", "waiting", "approve decision node @abc")
    assert len(c.msgs) == 1
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert len(c.msgs) == 1
    assert any("i0: waiting on the user" in a for a in
               watch.render.summary(plan, store.read(d), config.load(), T0)["attention"])
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert len(c.msgs) == 1
    engine.report(plan, d, w0, "i0", "running", "approved")
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    engine.report(plan, d, w0, "i0", "waiting", "approve the second node")
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert len(c.msgs) == 2 and "approve the second node" in c.msgs[-1][2]


def test_pm_waiting_report_does_not_echo_back_from_watcher(tmp_path):
    d = _setup(tmp_path, autolaunch=False, items=1)
    plan = watch.load(d / "plan.toml", "p")
    c, ran = FakeCoord(), []
    pm_world = engine.World(coord=c, now=lambda: T0, pane=lambda: "%1")
    engine.attach(d, pm_world, str(tmp_path), "pm-x", pane="%1")
    engine.adopt(plan, d, pm_world, "i0", "s1", "", "", "")
    engine.report(plan, d, pm_world, "i0", "waiting", "Daniel must choose")
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert c.msgs == []


def test_merge_queue_notifies_only_for_new_members(tmp_path):
    d = _setup(tmp_path, autolaunch=False)
    plan_file = d / "plan.toml"
    plan_file.write_text(plan_file.read_text().replace(str(tmp_path / "repos" / "r1"),
                                                  str(tmp_path / "repos" / "r0")).replace(
                                                      str(tmp_path / "repos" / "r2"),
                                                      str(tmp_path / "repos" / "r0")))
    plan = watch.load(plan_file, "p")
    c, ran = FakeCoord(), []
    pm_world = engine.World(coord=c, now=lambda: T0, pane=lambda: "%1")
    engine.attach(d, pm_world, str(tmp_path), "pm-x", pane="%1")
    for n in range(3):
        engine.adopt(plan, d, pm_world, f"i{n}", f"s{n}", "", "", "")
    for n in (0, 1):
        engine.report(plan, d, pm_world, f"i{n}", "review", f"PR {n}")
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert len(c.msgs) == 1 and "merges queue" in c.msgs[-1][2]
    engine.report(plan, d, pm_world, "i2", "review", "PR 2")
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert len(c.msgs) == 2 and "i0, i1, i2" in c.msgs[-1][2]
    engine.report(plan, d, pm_world, "i0", "running", "queued elsewhere")
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert len(c.msgs) == 2
    engine.report(plan, d, pm_world, "i0", "review", "PR 0")
    watch.tick(d, config.load(), _world(c, ran), oracles_due=False)
    assert len(c.msgs) == 3


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


def test_busy_box_is_one_episode_despite_clearings_and_reason_changes(tmp_path):
    d = _setup(tmp_path, autolaunch=False, items=1)
    Path(os.environ["ZAMU_CONFIG"]).write_text('admission = ["headroom"]\n')
    c, ran = FakeCoord(), []
    clock = [T0]
    result = [(75, "not now: cpu: tasks waited 41%", "")]

    def world():
        w = _world(c, ran)()
        w.now = lambda: clock[0]
        inner = w.run
        w.run = lambda argv: result[0] if argv[0] == "headroom" else inner(argv)
        return w

    assert len(watch.tick(d, config.load(), world, False).attention) == 1
    result[0] = (75, "not now: memory stalled 2%", "")
    assert watch.tick(d, config.load(), world, False).attention == []
    result[0] = (75, "not now: cpu: tasks waited 43%", "")
    assert watch.tick(d, config.load(), world, False).attention == []
    clock[0] += dt.timedelta(hours=1)
    assert len(watch.tick(d, config.load(), world, False).attention) == 1
    result[0] = (0, "admit", "")
    assert watch.tick(d, config.load(), world, False).attention == []
    result[0] = (75, "not now: memory stalled 3%", "")
    assert len(watch.tick(d, config.load(), world, False).attention) == 1


def test_busy_box_is_not_news_without_a_free_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("ZAMU_MAX_WORKERS", "1")
    d = _setup(tmp_path, autolaunch=False, items=2)
    plan = watch.load(d / "plan.toml", "p")
    engine.adopt(plan, d, engine.World(now=lambda: T0), "i0", "s1", "%1", "", "")
    Path(os.environ["ZAMU_CONFIG"]).write_text('admission = ["headroom"]\n')
    c, ran, checks = FakeCoord(), [], []

    def world():
        w = _world(c, ran)()
        inner = w.run
        def run(argv):
            if argv[0] == "headroom":
                checks.append(1)
                return 75, "not now: cpu", ""
            return inner(argv)
        w.run = run
        return w

    assert watch.tick(d, config.load(), world, False).attention == []
    assert checks == []


def test_successful_launch_rearms_busy_episode_after_pulse_said_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("ZAMU_MAX_WORKERS", "1")
    d = _setup(tmp_path, autolaunch=True, items=2)
    plan = watch.load(d / "plan.toml", "p")
    Path(os.environ["ZAMU_CONFIG"]).write_text('admission = ["headroom"]\n')
    c, ran = FakeCoord(), []
    clock = [T0]
    readings = [75, 75, 75, 0, 0, 75, 75]

    def world():
        w = _world(c, ran)()
        w.now = lambda: clock[0]
        inner = w.run
        def run(argv):
            if argv[0] == "headroom":
                return (75, "not now: cpu", "") if readings.pop(0) == 75 else (0, "yes", "")
            return inner(argv)
        w.run = run
        return w

    assert any("box is busy" in a for a in watch.tick(d, config.load(), world, False).attention)
    clock[0] += dt.timedelta(minutes=5)
    assert watch.tick(d, config.load(), world, False).launched[0].startswith("i0")
    engine.mark(plan, d, engine.World(now=lambda: clock[0]), "i0", "done", "finished")
    clock[0] += dt.timedelta(minutes=15)
    assert any("box is busy" in a for a in watch.tick(d, config.load(), world, False).attention)
    assert readings == []


def test_a_late_busy_read_does_not_wake_the_pm_as_a_launch_error(tmp_path):
    d = _setup(tmp_path, autolaunch=True, items=1)
    Path(os.environ["ZAMU_CONFIG"]).write_text('admission = ["headroom"]\n')
    c, ran, reads = FakeCoord(), [], []

    def world():
        w = _world(c, ran)()
        inner = w.run
        def run(argv):
            if argv[0] == "headroom":
                reads.append(1)
                return (75, "not now: cpu", "") if len(reads) % 3 == 0 else (0, "yes", "")
            return inner(argv)
        w.run = run
        return w

    first = watch.tick(d, config.load(), world, False)
    assert len(reads) == 3  # pulse and ready_to_fill admit; prepare gets the late busy read
    assert first.launched == [] and first.errors == []
    assert not first.news
    assert c.msgs == []


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


def _other_plan(tmp_path, name, count=1):
    d = config.plans_dir() / name
    d.mkdir(parents=True)
    body = '[plan]\nautolaunch = true\n'
    for n in range(count):
        make_repo(tmp_path, f'{name}{n}')
        body += (f'[items.i{n}]\ntitle="Item {n}"\nkind="build"\n'
                 f'repo="{tmp_path / "repos" / f"{name}{n}"}"\noracle="manual"\n')
    (d / 'plan.toml').write_text(body)
    return d


def test_watch_fills_three_plans_in_priority_order(tmp_path):
    low = _other_plan(tmp_path, 'low')
    middle = _other_plan(tmp_path, 'middle')
    high = _other_plan(tmp_path, 'high')
    cfg = config.Config(max_workers=2, priority={'high': 9, 'middle': 4})
    ticks = watch.cycle(None, cfg, _world(FakeCoord(), []), False, notify=False)
    assert {t.plan for t in ticks if t.launched} == {'high', 'middle'}
    assert not store.read(low)['items'].get('i0', {}).get('state')
    assert store.read(high)['items']['i0']['state'] == 'running'
    assert store.read(middle)['items']['i0']['state'] == 'running'


def test_reserve_beats_higher_priority_and_never_preempts(tmp_path):
    high = _other_plan(tmp_path, 'high', 2)
    reserved = _other_plan(tmp_path, 'reserved')
    cfg = config.Config(max_workers=2, priority={'high': 10}, reserve={'reserved': 1})
    watch.cycle(None, cfg, _world(FakeCoord(), []), False, notify=False)
    assert store.read(reserved)['items']['i0']['state'] == 'running'
    assert sum(r.get('state') == 'running' for r in store.read(high)['items'].values()) == 1
    # A later priority change does not displace either running worker.
    changed = config.Config(max_workers=2, priority={'reserved': 100}, reserve={'reserved': 2})
    watch.cycle(None, changed, _world(FakeCoord(), []), False, notify=False)
    assert sum(r.get('state') == 'running' for r in store.read(high)['items'].values()) == 1
    assert store.read(reserved)['items']['i0']['state'] == 'running'


def test_equal_priority_uses_oldest_ready_item(tmp_path):
    older = _other_plan(tmp_path, 'zolder')
    newer = _other_plan(tmp_path, 'anewer')
    with store.transaction(older) as doc:
        doc['items'].setdefault('i0', {})['ready_since'] = (T0 - dt.timedelta(days=1)).isoformat()
    with store.transaction(newer) as doc:
        doc['items'].setdefault('i0', {})['ready_since'] = T0.isoformat()
    ticks = watch.cycle(None, config.Config(max_workers=1), _world(FakeCoord(), []), False, notify=False)
    assert [t.plan for t in ticks if t.launched] == ['zolder']


def test_board_names_plan_that_took_priority_slot(tmp_path):
    from zamu import render
    low = _other_plan(tmp_path, 'low')
    _other_plan(tmp_path, 'high')
    cfg = config.Config(max_workers=1, priority={'high': 10})
    watch.cycle(None, cfg, _world(FakeCoord(), []), False, notify=False)
    board = render.board(watch.load(low / 'plan.toml', 'low'), store.read(low), cfg, T0)
    assert 'i0 (ready, waits for slot taken by higher-priority plan high)' in board


def test_priority_wait_is_hidden_when_engine_or_budget_also_blocks(tmp_path, monkeypatch):
    from zamu import render
    low = _other_plan(tmp_path, 'low')
    _other_plan(tmp_path, 'high')
    cfg = config.Config(max_workers=1, priority={'high': 10})
    watch.cycle(None, cfg, _world(FakeCoord(), []), False, notify=False)
    plan, doc = watch.load(low / 'plan.toml', 'low'), store.read(low)

    monkeypatch.setattr(engine, '_choose', lambda *_args: (None, 'claude: cap 1 reached'))
    board = render.board(plan, doc, cfg, T0)
    assert 'i0 (ready, waits for engine: claude: cap 1 reached)' in board
    assert 'slot taken by higher-priority plan' not in board

    budget_cfg = config.Config(max_workers=1, priority={'high': 10},
                               budgets={'claude': ('probe',)})
    monkeypatch.setattr(render.budgets, 'read', lambda: {'claude': {'verdict': 'not-now', 'data': {}}})
    board = render.board(plan, doc, budget_cfg, T0)
    assert 'i0 (ready, waits for budget (claude))' in board
    assert 'slot taken by higher-priority plan' not in board


def test_priority_wait_uses_plan_specific_cap(tmp_path):
    low = _other_plan(tmp_path, 'low', 2)
    high = _other_plan(tmp_path, 'high')
    (low / 'plan.toml').write_text((low / 'plan.toml').read_text().replace(
        '[plan]\n', '[plan]\nmax_workers = 1\n'))
    with store.transaction(low) as doc:
        doc['items']['i0'] = {'state': 'running', 'engine': 'claude', 'wid': 'low-worker'}
    with store.transaction(high) as doc:
        doc['items']['i0'] = {'state': 'running', 'engine': 'claude', 'wid': 'high-worker'}
    cfg = config.Config(max_workers=3, priority={'high': 10})
    watch.cycle(None, cfg, _world(FakeCoord(), []), False, notify=False)
    assert store.read(low)['priority_wait'] == {'i1': 'high'}


def test_cross_plan_fill_respects_max_workers_with_more_candidates(tmp_path):
    first = _other_plan(tmp_path, 'first', 2)
    second = _other_plan(tmp_path, 'second', 2)
    cfg = config.Config(max_workers=1)
    c, ran = FakeCoord(), []
    # The global scheduler can see several candidates even though only one fits.
    plan = watch.load(first / 'plan.toml', 'first')
    assert len(engine.ready_to_fill(plan, first, cfg, _world(c, ran)(), all_candidates=True)) == 2
    ticks = watch.cycle(None, cfg, _world(c, ran), False, notify=False)
    assert sum(len(t.launched) for t in ticks) == 1
    assert sum(r.get('state') == 'running' for d in (first, second)
               for r in store.read(d)['items'].values()) == 1
    assert len([a for a in ran if a[:2] == ['tmux', 'new-window']]) == 1


def test_all_plan_priority_includes_parked_workers_ready_to_resume(tmp_path):
    parked = _other_plan(tmp_path, 'parked')
    ready = _other_plan(tmp_path, 'ready')
    repo = tmp_path / 'repos' / 'parked0'
    tree = repo / '.worktrees' / 'i0'
    tree.mkdir(parents=True)
    with store.transaction(parked) as doc:
        doc['items']['i0'] = {
            'state': 'parked', 'engine': 'claude', 'session': 'sid', 'worktree': str(tree),
            'parked': {'at': T0.isoformat(), 'session': 'sid', 'asks': ['Q-1234'],
                       'asks_seen': ['Q-1234'], 'prior_state': 'running'},
        }
    c, ran = FakeCoord(), []
    c.open_asks = lambda _repo: set()
    cfg = config.Config(max_workers=1, priority={'parked': 10})
    ticks = watch.cycle(None, cfg, _world(c, ran), False, notify=False)
    assert [t.plan for t in ticks if t.launched] == ['parked']
    assert store.read(parked)['items']['i0']['state'] == 'running'
    assert store.read(ready)['items']['i0'].get('state') != 'running'
