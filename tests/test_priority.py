"""Box-level priority settings are validated, atomic, and audited."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from zamu import config
from zamu.cli import main


def test_priority_command_writes_config_and_audit(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('ZAMU_USER', 'Daniel')
    d = config.plans_dir() / 'alpha'
    d.mkdir(parents=True)
    (d / 'plan.toml').write_text('[items.a]\ntitle="a"\nkind="external"\noracle="manual"\n')
    p = Path(os.environ['ZAMU_CONFIG'])
    p.write_text('max_workers = 5\n[engines]\ncodex = ["codex"]\n')
    assert main(['priority', 'alpha', '7']) == 0
    assert main(['priority', '--reserve', 'alpha', '2']) == 0
    assert main(['priority']) == 0
    assert 'alpha: priority 7, reserve 2' in capsys.readouterr().out
    cfg = config.load()
    assert cfg.max_workers == 5 and cfg.engines['codex'] == ('codex',)
    assert cfg.priority == {'alpha': 7} and cfg.reserve == {'alpha': 2}
    events = [json.loads(line) for line in (p.parent / 'priority.jsonl').read_text().splitlines()]
    assert [(e['field'], e['old'], e['new']) for e in events] == \
        [('priority', 0, 7), ('reserve', 0, 2)]
    assert all(e['who'] == 'Daniel' and e['at'] for e in events)


@pytest.mark.parametrize('body', [
    '[priority]\nalpha = true\n',
    '[priority]\nalpha = "high"\n',
    '[reserve]\nalpha = -1\n',
    '[reserve]\nalpha = false\n',
])
def test_bad_weight_config_is_refused(body, tmp_path):
    Path(os.environ['ZAMU_CONFIG']).write_text(body)
    with pytest.raises(config.ConfigError):
        config.load()
