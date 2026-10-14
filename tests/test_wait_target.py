"""Named worker waits and the one-time signal when their target moves."""

import datetime as dt

import pytest

from zamu import cli, engine, render, store
from zamu.config import Config
from zamu.plan import parse


NOW = dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc)


class Asks:
    def __init__(self, open_ids):
        self.open_ids = open_ids

    def open_asks(self, repo):
        return self.open_ids

    def live_sessions(self, repo):
        return None


def setup(tmp_path):
    plan = parse(f'[items.work]\ntitle="Work"\nkind="build"\nrepo="{tmp_path}"\noracle="manual"\n', tmp_path / 'plan.toml')
    with store.transaction(tmp_path) as doc:
        doc['items']['work'] = {'state': 'running'}
    return plan


def world(tmp_path, asks=None):
    return engine.World(now=lambda: NOW, cwd=lambda: str(tmp_path), coord=Asks(asks))


def test_file_wait_fires_once_when_fingerprint_changes_and_clears_on_running(tmp_path):
    plan = setup(tmp_path)
    target = tmp_path / 'brief.md'
    target.write_text('first')
    w = world(tmp_path)
    engine.report(plan, tmp_path, w, 'work', 'waiting', 'peer is revising', on=str(target))
    doc = store.read(tmp_path)
    assert doc['items']['work']['wait_target']['value'] == str(target)
    assert 'waiting on ' + str(target) in render.board(plan, doc, Config(), NOW)
    assert 'waiting on the user' not in render.board(plan, doc, Config(), NOW)
    assert engine.pulse(plan, tmp_path, Config(escalate='none'), w)['changes'] == []
    target.write_text('second version')
    changed = engine.pulse(plan, tmp_path, Config(escalate='none'), w)['changes']
    assert changed == [f'work: {target} changed since it reported waiting on it; tell it to resume']
    assert engine.pulse(plan, tmp_path, Config(escalate='none'), w)['changes'] == []
    assert changed[0] in render.board(plan, store.read(tmp_path), Config(), NOW)
    engine.report(plan, tmp_path, w, 'work', 'running', 'resumed')
    assert 'wait_target' not in store.read(tmp_path)['items']['work']


def test_ask_wait_fires_after_observed_ask_closes_but_not_when_queue_is_unreadable(tmp_path):
    plan = setup(tmp_path)
    asks = {'Q-ABCD'}
    w = world(tmp_path, asks)
    engine.report(plan, tmp_path, w, 'work', 'waiting', 'peer answer', on='Q-ABCD')
    asks.clear()
    w.coord.open_ids = None
    assert engine.pulse(plan, tmp_path, Config(escalate='none'), w)['changes'] == []
    w.coord.open_ids = asks
    assert engine.pulse(plan, tmp_path, Config(escalate='none'), w)['changes'] == [
        'work: ask Q-ABCD closed since it reported waiting on it; tell it to resume']


def test_ask_filed_after_report_can_still_be_observed_closing(tmp_path):
    plan = setup(tmp_path)
    asks = set()
    w = world(tmp_path, asks)
    engine.report(plan, tmp_path, w, 'work', 'waiting', 'peer will file ask', on='Q-ABCD')
    asks.add('Q-ABCD')
    assert engine.pulse(plan, tmp_path, Config(escalate='none'), w)['changes'] == []
    asks.clear()
    assert engine.pulse(plan, tmp_path, Config(escalate='none'), w)['changes'] == [
        'work: ask Q-ABCD closed since it reported waiting on it; tell it to resume']


def test_pm_wait_is_separate_from_daniels_wait_and_session_target_is_rejected(tmp_path):
    plan = setup(tmp_path)
    w = world(tmp_path)
    engine.report(plan, tmp_path, w, 'work', 'waiting', 'queued to land', on='pm')
    board = render.board(plan, store.read(tmp_path), Config(), NOW)
    assert 'waiting on the PM' in board
    assert 'waiting on the user' not in board
    with pytest.raises(engine.EngineError, match='session targets'):
        engine.report(plan, tmp_path, w, 'work', 'waiting', 'peer reply', on='peer-session')


def test_report_cli_accepts_on_and_rejects_it_for_non_waiting_state():
    args = cli.build_parser().parse_args(['report', '--plan', 'p', 'work', 'waiting',
                                          'peer is editing', '--on', '/briefs/work.md'])
    assert args.on == '/briefs/work.md'


def test_unreadable_file_probe_keeps_wait_pending(tmp_path, monkeypatch):
    plan = setup(tmp_path)
    target = tmp_path / 'brief.md'
    target.write_text('first')
    w = world(tmp_path)
    engine.report(plan, tmp_path, w, 'work', 'waiting', 'peer editing', on=str(target))
    original = engine._file_fingerprint
    monkeypatch.setattr(engine, '_file_fingerprint', lambda path: None)
    assert engine.pulse(plan, tmp_path, Config(escalate='none'), w)['changes'] == []
    assert not store.read(tmp_path)['items']['work']['wait_target'].get('changed')
    monkeypatch.setattr(engine, '_file_fingerprint', original)
    target.write_text('second version')
    assert engine.pulse(plan, tmp_path, Config(escalate='none'), w)['changes']


def test_missing_relative_file_is_a_known_state_and_creation_moves_it(tmp_path):
    plan = setup(tmp_path)
    w = world(tmp_path)
    engine.report(plan, tmp_path, w, 'work', 'waiting', 'peer will write brief', on='brief.md')
    target = tmp_path / 'brief.md'
    assert store.read(tmp_path)['items']['work']['wait_target']['fingerprint'] == 'missing'
    target.write_text('ready')
    assert engine.pulse(plan, tmp_path, Config(escalate='none'), w)['changes'] == [
        f'work: {target} changed since it reported waiting on it; tell it to resume']


def test_new_blocker_or_claimed_done_clears_previous_named_wait(tmp_path):
    plan = setup(tmp_path)
    w = world(tmp_path)
    engine.report(plan, tmp_path, w, 'work', 'waiting', 'queued', on='pm')
    engine.report(plan, tmp_path, w, 'work', 'stuck', 'git is unavailable')
    rec = store.read(tmp_path)['items']['work']
    assert 'waiting' not in rec and 'wait_target' not in rec
    engine.report(plan, tmp_path, w, 'work', 'waiting', 'queued again', on='pm')
    engine.report(plan, tmp_path, w, 'work', 'done', 'work is complete')
    rec = store.read(tmp_path)['items']['work']
    assert 'waiting' not in rec and 'wait_target' not in rec
