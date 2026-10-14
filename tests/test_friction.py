"""Feedback and event-derived friction on the portfolio."""

import datetime as dt
import json
import shutil

import pytest

from zamu import cli, config, engine, feedback, portfolio, store
from zamu.config import Config
from zamu.engine import World
from zamu.plan import load


NOW = dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc)


def test_feedback_migrates_field_notes_once_and_lists_since(tmp_path, capsys, monkeypatch):
    source = tmp_path / "field-notes.md"
    source.write_text("# Notes\n- 2026-10-12 foreman1: First observation\n"
                      "  continued on next line\n- 2026-10-13 imbu-prj: Second observation\n")
    monkeypatch.setattr(feedback, "FIELD_NOTES", source)
    monkeypatch.setenv("TMUX_PANE", "%42")
    monkeypatch.setenv("ZAMU_SESSION_NAME", "worker-a")
    assert cli.main(["feedback", "--plan", "alpha", "A new observation"]) == 0
    assert cli.main(["feedback", "--list", "--since", "2026-10-13"]) == 0
    listed = capsys.readouterr().out
    assert "First observation" not in listed
    assert "Second observation" in listed and "A new observation" in listed
    rows = [json.loads(x) for x in (config.home() / "feedback.jsonl").read_text().splitlines()]
    assert len(rows) == 3
    assert rows[0]["text"] == "First observation continued on next line"
    assert rows[0]["name"] == "foreman1"
    assert rows[-1]["pane"] == "%42" and rows[-1]["name"] == "worker-a"
    assert cli.main(["feedback", "--list"]) == 0
    assert len((config.home() / "feedback.jsonl").read_text().splitlines()) == 3


def test_real_field_notes_copy_migrates_every_dated_bullet(tmp_path):
    if not feedback.FIELD_NOTES.exists():
        pytest.skip("local field notes are unavailable")
    copy = tmp_path / "field-notes.md"
    shutil.copyfile(feedback.FIELD_NOTES, copy)
    expected = sum(bool(feedback.NOTE.match(line)) for line in copy.read_text().splitlines())
    assert expected > 0
    assert feedback.migrate(copy) == expected
    assert feedback.migrate(copy) == 0
    assert len(feedback.list_feedback()) == expected


def test_feedback_with_same_timestamp_lists_newest_append_first(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "FIELD_NOTES", tmp_path / "missing")
    feedback.add("alpha", "first", NOW)
    feedback.add("alpha", "second", NOW)
    assert [row["text"] for row in feedback.list_feedback()] == ["second", "first"]


def test_feedback_rejects_blank_text(capsys):
    assert cli.main(["feedback", "--plan", "alpha", "   "]) == 2
    assert "feedback needs" in capsys.readouterr().err


def test_portfolio_friction_counts_week_and_previous_week(tmp_path, monkeypatch):
    root = config.plans_dir()
    d = root / "alpha"
    d.mkdir(parents=True)
    (d / "plan.toml").write_text('[plan]\n[items.a]\nkind="build"\ntitle="A"\nrepo="/repo"\noracle="manual"\n'
                                 '[items.b]\nkind="external"\ntitle="B"\noracle="manual"\nnag_after="1d"\n')
    (d / "state.json").write_text(json.dumps({"items": {"b": {"ready_since": "2026-10-12T00:00:00+00:00"}}, "pm": {"pane": "%1"}}))
    events = [
        ("2026-10-06T12:00:00+00:00", "adopt", {}),
        ("2026-10-13T09:00:00+00:00", "adopt", {}),
        ("2026-10-13T10:00:00+00:00", "worker", {"worker": "dead"}),
        ("2026-10-13T11:00:00+00:00", "worker", {"worker": "live"}),
        ("2026-10-13T12:00:00+00:00", "launch_hold", {"reason": "admission", "state": "start"}),
        ("2026-10-13T14:00:00+00:00", "launch_hold", {"reason": "admission", "state": "end"}),
        ("2026-10-14T09:00:00+00:00", "launch_hold", {"reason": "budget", "state": "start"}),
        ("2026-10-14T10:00:00+00:00", "launch_hold", {"reason": "budget", "state": "end"}),
        ("2026-10-14T10:00:00+00:00", "report", {"pane": "%1"}),
        ("2026-10-14T10:00:00+00:00", "verify", {"ok": False}),
        ("2026-10-14T10:00:00+00:00", "wait_nag", {}),
    ]
    (d / "events.jsonl").write_text("".join(json.dumps({"t": t, "item": "a", "what": what, **extra}) + "\n" for t, what, extra in events))
    monkeypatch.setattr(feedback, "FIELD_NOTES", tmp_path / "missing")
    feedback.add("alpha", "Recent feedback", NOW, pane="%2", name="worker")
    data = portfolio.snapshot(Config(), World(now=lambda: NOW, scan=lambda: [], run=lambda argv: (0, "active", "")), claude_projects=tmp_path / "none", codex_sessions=tmp_path / "none")
    current = data["friction"]["this_week"]
    previous = data["friction"]["previous_week"]
    assert current["adopted"] == 1 and previous["adopted"] == 1
    assert current["revived"] == 1 and current["admission_holds"] == 1
    assert current["admission_hours"] == 2 and current["budget_hours"] == 1
    assert current["pm_reports"] == 1 and current["verify_reopens"] == 1
    assert current["nagged_waits"] == 1
    assert data["friction"]["feedback"][0]["text"] == "Recent feedback"
    assert "friction this week" in portfolio.board(data)
    assert "adopted 1 (+0)" in portfolio.board(data)


def test_pulse_records_hold_duration_and_nag_once(tmp_path):
    d = tmp_path / "plan"
    d.mkdir()
    (d / "plan.toml").write_text('[plan]\n[items.a]\nkind="build"\ntitle="A"\nrepo="/repo"\noracle="manual"\nnag_after="1d"\n')
    plan = load(d / "plan.toml", name="plan")
    clock = [NOW]
    admitted = [False]
    world = World(now=lambda: clock[0], run=lambda argv: (0 if admitted[0] else 75, "busy", ""))
    cfg = Config(admission=("headroom",), escalate="none")
    engine.pulse(plan, d, cfg, world)
    clock[0] += dt.timedelta(days=2)
    engine.pulse(plan, d, cfg, world)
    admitted[0] = True
    clock[0] += dt.timedelta(hours=1)
    engine.pulse(plan, d, cfg, world)
    engine.pulse(plan, d, cfg, world)
    events = store.events(d)
    assert [e["state"] for e in events if e["what"] == "launch_hold"] == ["start", "end"]
    assert len([e for e in events if e["what"] == "wait_nag"]) == 1
    assert store.read(d).get("friction_holds") is None


def test_report_event_carries_reporting_pane(tmp_path):
    d = tmp_path / "plan"
    d.mkdir()
    (d / "plan.toml").write_text('[plan]\n[items.a]\nkind="external"\ntitle="A"\noracle="manual"\n')
    plan = load(d / "plan.toml", name="plan")
    engine.report(plan, d, World(now=lambda: NOW, pane=lambda: "%7"), "a", "running", "working")
    assert store.events(d)[0]["pane"] == "%7"


def test_budget_hold_emits_one_start_and_end(tmp_path):
    d = tmp_path / "plan"
    d.mkdir()
    (d / "plan.toml").write_text('[plan]\n[items.a]\nkind="build"\ntitle="A"\nrepo="/repo"\noracle="manual"\n')
    plan = load(d / "plan.toml", name="plan")
    verdict = [75]
    world = World(now=lambda: NOW, budget_run=lambda argv: (verdict[0], "{}", ""))
    cfg = Config(budgets={"claude": ("probe",)}, budget_refresh_minutes=0, escalate="none")
    engine.pulse(plan, d, cfg, world)
    engine.pulse(plan, d, cfg, world)
    verdict[0] = 0
    engine.pulse(plan, d, cfg, world)
    assert [(e["reason"], e["state"]) for e in store.events(d) if e["what"] == "launch_hold"] == [
        ("budget", "start"), ("budget", "end")]


def test_unreadable_event_log_is_unknown_in_portfolio(tmp_path, monkeypatch):
    d = config.plans_dir() / "alpha"
    d.mkdir(parents=True)
    (d / "plan.toml").write_text('[plan]\n[items.a]\nkind="external"\ntitle="A"\noracle="manual"\n')
    (d / "events.jsonl").write_text("broken\n" + json.dumps({"t": NOW.isoformat(), "what": "adopt", "item": "a"}) + "\n")
    monkeypatch.setattr(feedback, "FIELD_NOTES", tmp_path / "missing")
    data = portfolio.snapshot(Config(), World(now=lambda: NOW, scan=lambda: [], run=lambda argv: (0, "active", "")), claude_projects=tmp_path / "none", codex_sessions=tmp_path / "none")
    assert data["friction"]["unknown"] is True
    assert "event log ?" in portfolio.board(data)


def test_admission_hold_excludes_item_already_blocked_by_engine_cap(tmp_path):
    d = tmp_path / "plan"
    d.mkdir()
    (d / "plan.toml").write_text('[plan]\n[items.a]\nkind="build"\ntitle="A"\nrepo="/repo"\noracle="manual"\n'
                                 '[items.b]\nkind="build"\ntitle="B"\nrepo="/repo"\noracle="manual"\nengine="codex"\n')
    plan = load(d / "plan.toml", name="plan")
    cfg = Config(admission=("headroom",), engines={"codex": ("codex",)},
                 engine_caps={"codex": 0}, escalate="none")
    engine.pulse(plan, d, cfg, World(now=lambda: NOW, run=lambda argv: (75, "busy", "")))
    assert [e["item"] for e in store.events(d) if e["what"] == "launch_hold"] == ["a"]
