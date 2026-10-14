"""PM messages use the recorded worker identity and session-coord's inbox."""

from __future__ import annotations

import datetime as dt

import pytest

from zamu import cli, engine, render, store
from zamu.coord import Coord
from zamu.plan import parse

NOW = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone.utc)


class FakeCoord:
    def __init__(self, rows, result=(True, "filed for worker")):
        self.rows = rows
        self.result = result
        self.sent = []

    def board(self, repo):
        return self.rows

    def msg(self, repo, target, message):
        self.sent.append((repo, target, message))
        return self.result


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "plan.toml"
    path.write_text('[items.a]\ntitle="A"\nkind="build"\nrepo="/r"\noracle="manual"\n')
    plan = parse(path.read_text(), path)
    return plan, tmp_path


def recorded(setup, engine_name, session, pane):
    _, d = setup
    with store.transaction(d) as doc:
        doc["items"]["a"] = {"state": "running", "wid": "worker-1", "engine": engine_name,
                             "session": session, "pane": pane}
        doc["workers"] = {"worker-1": {"engine": engine_name, "session": session,
                                       "pane": pane, "items": ["a"]}}


def test_claude_tell_uses_recorded_session_and_records_visible_event(setup):
    recorded(setup, "claude", "sid-1", "%5")
    coord = FakeCoord([{"session_id": "sid-1", "pane": "%5", "engine": "claude"}])
    world = engine.World(coord=coord, now=lambda: NOW, pane_exists=lambda pane: True)
    plan, d = setup
    text = "Please check the change in the dependency before landing. " + "x" * 50

    result = engine.tell(plan, d, world, "a", text)

    assert "filed" in result
    assert coord.sent == [("/r", "sid-1", text)]
    events = store.events(d, "a")
    assert events[-1]["what"] == "tell"
    assert events[-1]["t"] == NOW.isoformat()
    assert events[-1]["text"] == text[:80]
    assert f"last told {NOW.isoformat()}: {text[:80]}" in render.show(
        plan, store.read(d), "a", events)


def test_codex_tell_resolves_worker_id_to_registered_pane(setup):
    recorded(setup, "codex", "", "%9")
    coord = FakeCoord([{"session_id": "codex-sid", "pane": "%9", "engine": "codex"}])
    world = engine.World(coord=coord, now=lambda: NOW, pane_exists=lambda pane: True)
    plan, d = setup

    engine.tell(plan, d, world, "worker-1", "Review the tests")

    assert coord.sent == [("/r", "%9", "Review the tests")]
    assert store.events(d, "a")[-1]["what"] == "tell"


def test_codex_tell_prefers_recorded_session_to_pane(setup):
    recorded(setup, "codex", "", "%9")
    plan, d = setup
    with store.transaction(d) as doc:
        doc["items"]["a"]["codex_session"] = "codex-sid"
    coord = FakeCoord([{"session_id": "codex-sid", "pane": "%9", "engine": "codex"}])
    world = engine.World(coord=coord, now=lambda: NOW, pane_exists=lambda pane: True)

    engine.tell(plan, d, world, "a", "Review the tests")

    assert coord.sent == [("/r", "codex-sid", "Review the tests")]


def test_tell_refuses_a_pane_registered_in_a_different_worktree(setup):
    recorded(setup, "codex", "", "%9")
    plan, d = setup
    with store.transaction(d) as doc:
        doc["items"]["a"]["worktree"] = "/r/.worktrees/a"
    coord = FakeCoord([{"session_id": "other", "pane": "%9", "engine": "codex",
                        "workdir": "/r/.worktrees/other"}])
    world = engine.World(coord=coord, now=lambda: NOW, pane_exists=lambda pane: True)

    with pytest.raises(engine.EngineError, match="worktree changed"):
        engine.tell(plan, d, world, "a", "Hello")

    assert coord.sent == []


@pytest.mark.parametrize("rows,reason", [([], "not registered"), (None, "could not check")])
def test_tell_refuses_unregistered_or_unreadable_board_without_sending(setup, rows, reason):
    recorded(setup, "claude", "sid-1", "%5")
    coord = FakeCoord(rows)
    world = engine.World(coord=coord, now=lambda: NOW, pane_exists=lambda pane: True)
    plan, d = setup

    with pytest.raises(engine.EngineError, match=reason):
        engine.tell(plan, d, world, "a", "Hello")

    assert coord.sent == []
    assert store.events(d, "a") == []


def test_tell_refuses_dead_pane_even_if_board_has_stale_record(setup):
    recorded(setup, "codex", "", "%9")
    coord = FakeCoord([{"session_id": "codex-sid", "pane": "%9", "engine": "codex"}])
    world = engine.World(coord=coord, now=lambda: NOW, pane_exists=lambda pane: False)
    plan, d = setup

    with pytest.raises(engine.EngineError, match="pane %9 is gone"):
        engine.tell(plan, d, world, "a", "Hello")

    assert coord.sent == []
    assert store.events(d, "a") == []


def test_tell_refuses_when_pane_cannot_be_checked(setup):
    recorded(setup, "codex", "", "%9")
    coord = FakeCoord([{"session_id": "codex-sid", "pane": "%9", "engine": "codex"}])
    world = engine.World(coord=coord, now=lambda: NOW, pane_exists=lambda pane: None)
    plan, d = setup

    with pytest.raises(engine.EngineError, match="could not check pane %9"):
        engine.tell(plan, d, world, "a", "Hello")

    assert coord.sent == []


def test_tell_refuses_session_coord_delivery_failure(setup):
    recorded(setup, "claude", "sid-1", "%5")
    coord = FakeCoord([{"session_id": "sid-1", "pane": "%5"}],
                      (False, "no inbox for sid-1"))
    world = engine.World(coord=coord, now=lambda: NOW, pane_exists=lambda pane: True)
    plan, d = setup

    with pytest.raises(engine.EngineError, match="no inbox for sid-1"):
        engine.tell(plan, d, world, "a", "Hello")

    assert store.events(d, "a") == []


def test_tell_refuses_unregistered_direct_send_confirmation(setup):
    recorded(setup, "claude", "sid-1", "%5")
    coord = FakeCoord([{"session_id": "sid-1", "pane": "%5"}],
                      (True, "sent to sid-1 -- unregistered, so typed rather than filed"))
    world = engine.World(coord=coord, now=lambda: NOW, pane_exists=lambda pane: True)
    plan, d = setup

    with pytest.raises(engine.EngineError, match="unregistered"):
        engine.tell(plan, d, world, "a", "Hello")

    assert store.events(d, "a") == []


def test_cli_tell_accepts_plan_and_item(setup, monkeypatch, capsys):
    recorded(setup, "claude", "sid-1", "%5")
    coord = FakeCoord([{"session_id": "sid-1", "pane": "%5"}])
    monkeypatch.setattr(cli, "world", lambda: engine.World(
        coord=coord, now=lambda: NOW, pane_exists=lambda pane: True))
    _, d = setup

    assert cli.main(["tell", "--plan", str(d), "a", "Check the diff"]) == 0

    assert "told worker-1" in capsys.readouterr().out
    assert coord.sent == [("/r", "sid-1", "Check the diff")]


def test_coord_reports_filed_mail_even_when_doorbell_refusal_is_on_stderr():
    coord = Coord(run=lambda argv: (0, "", "filed for worker, but the doorbell could not be rung"),
                  binary="session-coord")

    assert coord.msg("/r", "sid-1", "Hello") == (
        True, "filed for worker, but the doorbell could not be rung")
