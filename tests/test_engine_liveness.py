"""Liveness: session-coord's board first, the recorded pane second, never a guess."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from zamu import engine, store
from zamu.config import Config
from zamu.coord import Coord
from zamu.plan import parse

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
    with store.transaction(tmp_path) as doc:  # as if zamu had launched it: pane proof applies
        doc["items"]["a"]["ours"] = True
    return plan, tmp_path


@pytest.mark.parametrize("board,pane,want", [
    ({"sid-1"}, None, "live"),        # session-coord lists it
    (set(), True, "live"),            # board misses it, but the pane still runs that session
    (set(), False, "dead"),           # neither
    (set(), None, "dead"),            # board looked and it is absent; pane could not be read
    (None, True, "live"),             # board unreadable, pane proves it
    (None, None, "unknown"),          # nothing could be read: never call it dead
    (None, False, "dead"),            # board unreadable, but tmux says the token is gone
])
def test_worker_liveness(setup, board, pane, want):
    plan, d = setup
    w = engine.World(coord=FakeCoord(board), now=lambda: T0 + dt.timedelta(hours=1),
                     pane_has_session=lambda p, s: pane)
    engine.pulse(plan, d, Config(), w)
    assert store.read(d)["items"]["a"]["worker"] == want


def test_the_pane_proves_nothing_for_a_worker_zamu_did_not_launch(setup):
    """A hand-started claude's argv carries no token, so a pane lookup cannot tell."""
    plan, d = setup
    with store.transaction(d) as doc:
        doc["items"]["a"].pop("ours")
    w = engine.World(coord=FakeCoord(None), now=lambda: T0 + dt.timedelta(hours=1),
                     pane_has_session=lambda p, s: False)
    engine.pulse(plan, d, Config(), w)
    assert store.read(d)["items"]["a"]["worker"] == "unknown"


def test_a_codex_window_is_judged_by_its_pane(tmp_path):
    plan = parse('[items.a]\ntitle="A"\nkind="build"\nrepo="/r"\noracle="manual"\n',
                 tmp_path / "plan.toml")
    w0 = engine.World(now=lambda: T0, pane_exists=lambda pane: True)
    engine.worker_add(tmp_path, w0, "cx1", "codex", pane="%44", worktree="/r")
    engine.assign(plan, tmp_path, w0, "a", "cx1")
    for alive, want in ((True, "live"), (False, "dead")):
        w = engine.World(coord=FakeCoord(None), now=lambda: T0 + dt.timedelta(hours=1),
                         pane_exists=lambda p, a=alive: a)
        engine.pulse(plan, tmp_path, Config(), w)
        assert store.read(tmp_path)["items"]["a"]["worker"] == want


def test_a_subagent_worker_is_unknown_and_holds_a_slot(tmp_path):
    plan = parse('[items.a]\ntitle="A"\nkind="build"\nrepo="/r"\noracle="manual"\n'
                 '[items.b]\ntitle="B"\nkind="build"\nrepo="/r"\noracle="manual"\n',
                 tmp_path / "plan.toml")
    w0 = engine.World(now=lambda: T0)
    engine.worker_add(tmp_path, w0, "sub1", "subagent")
    engine.assign(plan, tmp_path, w0, "a", "sub1")
    engine.assign(plan, tmp_path, w0, "b", "sub1")
    engine.pulse(plan, tmp_path, Config(), engine.World(coord=FakeCoord(set()), now=lambda: T0))
    doc = store.read(tmp_path)
    assert doc["items"]["a"]["worker"] == "unknown"
    assert engine.slots(plan, doc, Config())[0] == 1  # one worker, two items, one slot


def test_an_adopted_worker_whose_pane_carries_its_session_id_is_live(setup):
    """foreman1's G14/G17/G18: hand-run launches, adopted, claude as the pane process itself,
    invisible to session-coord. The id in the pane's argv is positive proof."""
    plan, d = setup
    with store.transaction(d) as doc:
        doc["items"]["a"].pop("ours")
    w = engine.World(coord=FakeCoord(set()), now=lambda: T0 + dt.timedelta(hours=1),
                     pane_has_session=lambda p, s: s == "sid-1")
    engine.pulse(plan, d, Config(), w)
    assert store.read(d)["items"]["a"]["worker"] == "live"
