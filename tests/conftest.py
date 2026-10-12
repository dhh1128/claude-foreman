"""Fixtures for the foreman suite.

Run with: uv run --no-project --python 3.11 --with pytest python -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """No test may read or write the real state root, config, or addendum."""
    monkeypatch.setenv("FOREMAN_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("FOREMAN_ADDENDUM", str(tmp_path / "addendum.md"))
    monkeypatch.delenv("FOREMAN_PLAN", raising=False)
    monkeypatch.delenv("FOREMAN_MAX_WORKERS", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
