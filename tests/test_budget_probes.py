import datetime as dt
import json

import pytest

from zamu import budget_probes as probes


NOW = dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc)


def rollout(root, limits):
    root.mkdir(exist_ok=True)
    (root / 'one.jsonl').write_text(json.dumps({'payload': {'rate_limits': limits}}) + '\n')


@pytest.mark.parametrize('limits, expected', [
    ({'primary': {'used_percent': 20, 'resets_at': NOW.timestamp() + 3600}}, 0),
    ({'primary': {'used_percent': 70, 'resets_at': NOW.timestamp() + 3600}}, 75),
    ({'rate_limit_reached_type': 'primary'}, 75),
])
def test_codex_quota_guard(tmp_path, limits, expected):
    rollout(tmp_path, limits)
    assert probes.codex(tmp_path, 60, NOW)[0] == expected


def test_codex_missing_rollout_is_unknown(tmp_path):
    assert probes.codex(tmp_path, 60, NOW)[0] == 2


def test_codex_searches_past_newer_rollout_without_limits(tmp_path):
    rollout(tmp_path, {'primary': {'used_percent': 70,
                                    'resets_at': NOW.timestamp() + 3600}})
    newer = tmp_path / 'newer.jsonl'
    newer.write_text('{"payload":{"token_count":{}}}\n')
    import os
    os.utime(tmp_path / 'one.jsonl', (NOW.timestamp() - 1, NOW.timestamp() - 1))
    os.utime(newer, (NOW.timestamp() + 1, NOW.timestamp() + 1))
    assert probes.codex(tmp_path, 60, NOW)[0] == 75


@pytest.mark.parametrize('remaining, expected', [(10, 0), (2, 75), (None, 2)])
def test_openrouter_remaining_credit(tmp_path, remaining, expected):
    def get(path, key):
        if remaining is None:
            raise OSError('offline')
        return {'data': {'limit': 20, 'limit_remaining': remaining}}
    assert probes.openrouter(5, key='test', get=get)[0] == expected


def test_openrouter_unlimited_key_uses_credits():
    paths = []
    def get(path, key):
        paths.append(path)
        return {'data': {'limit': None}} if path == '/key' else {
            'data': {'total_credits': 12, 'total_usage': 9}}
    assert probes.openrouter(5, key='test', get=get)[0] == 75
    assert paths == ['/key', '/credits']


@pytest.mark.parametrize('tokens, expected', [(10, 0), (100, 75)])
def test_claude_estimate_counts_message_once(tmp_path, tokens, expected):
    root = tmp_path / 'projects'
    root.mkdir()
    row = {'timestamp': NOW.isoformat(), 'message': {'id': 'm1', 'model': 'claude-haiku',
           'usage': {'input_tokens': tokens, 'output_tokens': 0}}}
    (root / 'session.jsonl').write_text(json.dumps(row) + '\n' + json.dumps(row) + '\n')
    assert probes.claude(root, 85, 100, NOW)[0] == expected


def test_claude_missing_transcripts_is_unknown(tmp_path):
    assert probes.claude(tmp_path / 'missing', 85, 100, NOW)[0] == 2


def test_claude_calibration_saves_allowance(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HOME', str(tmp_path))
    root = tmp_path / 'projects'
    root.mkdir()
    row = {'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(),
           'message': {'id': 'm1', 'model': 'claude-haiku',
                       'usage': {'input_tokens': 100, 'output_tokens': 0}}}
    (root / 'session.jsonl').write_text(json.dumps(row) + '\n')
    assert probes.main(['claude', '--projects', str(root), 'calibrate', '50']) == 0
    assert '"estimate": true' in capsys.readouterr().out
    assert 'allowance = 200.0' in (tmp_path / '.config/budget/claude.toml').read_text()
