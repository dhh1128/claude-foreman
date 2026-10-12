"""Fixtures for the zamu suite.

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
    monkeypatch.setenv("ZAMU_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ZAMU_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("ZAMU_ADDENDUM", str(tmp_path / "addendum.md"))
    monkeypatch.delenv("ZAMU_PLAN", raising=False)
    monkeypatch.delenv("ZAMU_MAX_WORKERS", raising=False)
    for n in ("HOME", "CONFIG", "ADDENDUM", "PLAN", "MAX_WORKERS", "USER"):
        monkeypatch.delenv(f"FOREMAN_{n}", raising=False)
    monkeypatch.delenv("ZAMU_USER", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
