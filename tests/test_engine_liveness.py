"""Liveness: session-coord's board first, the recorded pane second, never a guess."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from foreman import engine, store
from foreman.config import Config
from foreman.coord import Coord
from foreman.plan import parse

T0 = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.timezone.utc)


class FakeCoord(Coord):
    def __init__(self, live):
        self.live = live

    def live_sessions(self, repo):
        return self.live

    def ask_add(self, *a, **k):
        return True, "ok"


@pytest.fixture
def setup(tmp_path):
    plan = parse('[items.a]\ntitle="A"\nkind="build"\nrepo="/r"\noracle="manual"\n',
                 tmp_path / "plan.toml")
    engine.adopt(plan, tmp_path, engine.World(now=lambda: T0), "a", "sid-1", "%5", "/r", "")
    return plan, tmp_path


@pytest.mark.parametrize("board,pane,want", [
    ({"sid-1"}, None, "live"),        # session-coord lists it
    (set(), True, "live"),            # board misses it, but the pane still runs that session
    (set(), False, "dead"),           # neither
    (set(), None, "dead"),            # board looked and it is absent; pane could not be read
    (None, True, "live"),             # board unreadable, pane proves it
    (None, None, "unknown"),          # nothing could be read: never call it dead
    (None, False, "unknown"),
])
def test_worker_liveness(setup, board, pane, want):
    plan, d = setup
    w = engine.World(coord=FakeCoord(board), now=lambda: T0 + dt.timedelta(hours=1),
                     pane_has_session=lambda p, s: pane)
    engine.pulse(plan, d, Config(), w)
    assert store.read(d)["items"]["a"]["worker"] == want
