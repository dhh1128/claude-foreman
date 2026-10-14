"""Budget readings govern engine choice without turning an unreadable probe into zero use."""

import datetime as dt
import json

from zamu import budgets, engine, render, store
from zamu import config
from zamu.config import Config
from zamu.plan import parse


NOW = dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc)


def setup(tmp_path):
    plan = parse('[items.a]\ntitle="A"\nkind="build"\nrepo="/r"\n'
                 'oracle="manual"\nengine=["codex", "claude"]\n',
                 tmp_path / 'plan.toml')
    cfg = Config(engines={'codex': ('codex',), 'claude': ('claude',)},
                 budgets={'codex': ('probe', 'codex'), 'claude': ('probe', 'claude')})
    return plan, cfg


def test_preference_falls_through_a_refused_budget(tmp_path, monkeypatch):
    plan, cfg = setup(tmp_path)
    monkeypatch.setattr('zamu.launch.worktree_exists', lambda p: True)
    seen = []

    def run(argv):
        seen.append(tuple(argv))
        if argv == ['probe', 'codex']:
            return 75, json.dumps({'figure': '61%', 'used_pct': 61}), ''
        return 0, json.dumps({'figure': '20%', 'used_pct': 20}), ''

    w = engine.World(run=run, now=lambda: NOW)
    assert engine.ready_to_fill(plan, tmp_path, cfg, w) == ['a']
    (launch,) = engine.prepare(plan, tmp_path, cfg, w, ['a'])
    assert launch.engine == 'claude'
    assert ('probe', 'codex') in seen


def test_unknown_first_engine_yields_to_a_known_admission(tmp_path, monkeypatch):
    plan, cfg = setup(tmp_path)
    monkeypatch.setattr('zamu.launch.worktree_exists', lambda p: True)
    w = engine.World(run=lambda a: (2, '{}', 'offline') if a[-1] == 'codex' else
                     (0, '{}', ''), now=lambda: NOW)
    (launch,) = engine.prepare(plan, tmp_path, cfg, w, ['a'])
    assert launch.engine == 'claude'


def test_unknown_budget_only_wins_when_no_engine_admits(tmp_path, monkeypatch):
    plan, cfg = setup(tmp_path)
    monkeypatch.setattr('zamu.launch.worktree_exists', lambda p: True)
    w = engine.World(run=lambda a: (2, '', 'unreadable') if a[-1] == 'codex' else
                     (75, '{}', ''), now=lambda: NOW)
    (launch,) = engine.prepare(plan, tmp_path, cfg, w, ['a'])
    assert launch.engine == 'codex'


def test_all_refused_budgets_leave_item_ready_with_reason(tmp_path):
    plan, cfg = setup(tmp_path)
    w = engine.World(run=lambda a: (75, json.dumps({'figure': 'spent'}), ''), now=lambda: NOW)
    assert engine.ready_to_fill(plan, tmp_path, cfg, w) == []
    board = render.board(plan, store.read(tmp_path), cfg, NOW)
    assert 'a (ready, waits for budget (codex, claude))' in board


def test_box_unknown_still_uses_count_cap(tmp_path):
    plan, _ = setup(tmp_path)
    cfg = Config(admission=('headroom',))
    w = engine.World(run=lambda a: (2, '', 'failed'), now=lambda: NOW)
    assert engine.ready_to_fill(plan, tmp_path, cfg, w) == ['a']


def test_commit_records_selected_engine_and_rechecks_budget(tmp_path, monkeypatch):
    plan, cfg = setup(tmp_path)
    monkeypatch.setattr('zamu.launch.worktree_exists', lambda p: True)
    monkeypatch.setattr('zamu.launch.execute', lambda ln, run: {'pane': '%1'})
    verdict = {'codex': 75, 'claude': 0}

    def run(argv):
        return verdict[argv[-1]], '{}', ''

    w = engine.World(run=run, now=lambda: NOW)
    (launch,) = engine.prepare(plan, tmp_path, cfg, w, ['a'])
    assert launch.engine == 'claude'
    verdict['claude'] = 75
    import pytest
    with pytest.raises(engine.BusyError):
        engine.commit_launch(plan, tmp_path, cfg, w, launch)
    verdict['claude'] = 0
    engine.commit_launch(plan, tmp_path, cfg, w, launch)
    assert store.read(tmp_path)['items']['a']['engine'] == 'claude'


def test_cap_holder_is_named_on_board_and_launch(tmp_path):
    plan = parse('[items.a]\ntitle="A"\nkind="build"\nrepo="/r"\noracle="manual"\n'
                 '[items.b]\ntitle="B"\nkind="build"\nrepo="/r"\noracle="manual"\n',
                 tmp_path / 'plan.toml')
    cfg = Config(engine_caps={'claude': 1})
    with store.transaction(tmp_path) as doc:
        doc['items']['a'] = {'state': 'running', 'worker': 'live', 'session': 's1',
                             'engine': 'claude'}
    board = render.board(plan, store.read(tmp_path), cfg, NOW)
    assert 'b: claude cap 1 held by a' in board
    assert 'b (ready, waits for engine: claude: cap 1 reached)' in board


def test_budget_tradeoff_is_one_ask_until_admission_returns(tmp_path):
    plan, cfg = setup(tmp_path)
    asks = []

    class Coord:
        def ask_add(self, repo, tag, kind, message):
            asks.append((tag, message))
            return True, 'filed'

    w = engine.World(coord=Coord(), now=lambda: NOW, cwd=lambda: '/r')
    doc = store.empty()
    spent = {e: {'verdict': 'not-now', 'data': {'figure': 'spent',
             'resets_at': (NOW + dt.timedelta(days=1)).isoformat()}} for e in cfg.budgets}
    engine._budget_episode(plan, tmp_path, doc, cfg, w, NOW, spent)
    assert not asks
    later = NOW + dt.timedelta(minutes=61)
    engine._budget_episode(plan, tmp_path, doc, cfg, w, later, spent)
    assert len(asks) == 1 and 'a (codex, claude)' in asks[0][1]
    engine._budget_episode(plan, tmp_path, doc, cfg, w, later, spent)
    assert len(asks) == 1
    doc['admission'] = {'ok': False, 'why': 'busy'}
    engine._budget_episode(plan, tmp_path, doc, cfg, w, later, spent)
    doc.pop('admission')
    engine._budget_episode(plan, tmp_path, doc, cfg, w, later, spent)
    assert len(asks) == 1
    recovered = {**spent, 'codex': {'verdict': 'admit', 'data': {}}}
    engine._budget_episode(plan, tmp_path, doc, cfg, w, later, recovered)
    engine._budget_episode(plan, tmp_path, doc, cfg, w, later, spent)
    engine._budget_episode(plan, tmp_path, doc, cfg, w, later + dt.timedelta(minutes=61), spent)
    assert len(asks) == 2 and asks[0][0] != asks[1][0]


def test_full_machine_does_not_start_budget_tradeoff_episode(tmp_path):
    plan = parse('[items.a]\ntitle="A"\nkind="build"\nrepo="/r"\noracle="manual"\n'
                 '[items.b]\ntitle="B"\nkind="build"\nrepo="/r"\noracle="manual"\n'
                 'engine=["codex", "claude"]\n', tmp_path / 'plan.toml')
    _old_plan, base_cfg = setup(tmp_path)
    cfg = Config(max_workers=1, engines=base_cfg.engines, budgets=base_cfg.budgets)
    doc = store.empty()
    doc['items']['a'] = {'state': 'running', 'worker': 'live', 'session': 's1',
                         'engine': 'claude'}
    readings = {e: {'verdict': 'not-now', 'data': {}} for e in cfg.budgets}
    assert engine.budget_waiting(plan, doc, cfg, readings) == []


def test_busy_box_does_not_start_budget_tradeoff_episode(tmp_path):
    plan, cfg = setup(tmp_path)
    doc = store.empty()
    doc['admission'] = {'ok': False, 'why': 'memory pressure'}
    readings = {e: {'verdict': 'not-now', 'data': {}} for e in cfg.budgets}
    assert engine.budget_waiting(plan, doc, cfg, readings) == []


def test_shared_account_command_is_probed_once_and_cached(tmp_path):
    calls = []
    cfg = Config(budgets={'codex': ('probe',), 'codex_fast': ('probe',)})
    def run(argv):
        calls.append(argv)
        return 0, '{"figure":"20%"}', ''
    readings = budgets.refresh(cfg, NOW, run)
    assert readings['codex'] == readings['codex_fast']
    budgets.refresh(cfg, NOW + dt.timedelta(minutes=1), run)
    assert calls == [['probe']]


def test_budget_config_reads_probe_command(tmp_path):
    config.config_path().write_text('[budgets.codex]\nprobe = ["budget", "codex", "--max-pct", "60"]\n')
    assert config.load().budgets['codex'] == ('budget', 'codex', '--max-pct', '60')
