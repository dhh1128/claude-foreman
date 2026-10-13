"""Reports appended by a worker inside its worktree."""

from __future__ import annotations

import datetime as dt

import pytest

from zamu import engine, render, store
from zamu.config import Config
from zamu.coord import Coord
from zamu.plan import parse

T0 = dt.datetime(2026, 10, 13, 12, tzinfo=dt.timezone.utc)


class Messages(Coord):
    def __init__(self):
        self.sent = []

    def msg(self, repo, target, text):
        self.sent.append((repo, target, text))
        return True


def setup(tmp_path, oracle="manual"):
    wt = tmp_path / "tree"
    wt.mkdir()
    plan = parse(f'[items.a]\ntitle="A"\nkind="build"\nrepo="{tmp_path}"\noracle="{oracle}"\n'
                 f'[items.b]\ntitle="B"\nkind="build"\nrepo="{tmp_path}"\noracle="manual"\n',
                 tmp_path / "plan.toml")
    with store.transaction(tmp_path) as doc:
        doc["items"]["a"] = {"state": "running", "worktree": str(wt)}
        doc["items"]["b"] = {"state": "done", "worktree": str(tmp_path / "other")}
        doc["pm"] = {"repo": str(tmp_path), "target": "pm"}
    (wt / ".zamu").mkdir()
    coord = Messages()
    world = engine.World(now=lambda: T0, coord=coord, oracle_run=lambda *args: (1, "no"))
    return plan, wt / ".zamu" / "reports", world, coord


def pulse(plan, d, world):
    return engine.pulse(plan, d, Config(escalate="none"), world)


def test_a_line_becomes_a_report_and_is_applied_once(tmp_path):
    plan, reports, world, coord = setup(tmp_path)
    reports.write_text("2026-10-13T12:00:00+00:00 review PR open\n")
    pulse(plan, tmp_path, world)
    first = store.read(tmp_path)["items"]["a"]
    assert first["state"] == "review" and first["last_line"] == "PR open"
    assert first["report_offset"] == reports.stat().st_size
    assert len(coord.sent) == 1
    pulse(plan, tmp_path, world)
    assert len(coord.sent) == 1
    assert len([e for e in store.events(tmp_path, "a") if e["what"] == "report"]) == 1


def test_crash_before_state_write_replays_whole_line(tmp_path, monkeypatch):
    plan, reports, world, coord = setup(tmp_path)
    reports.write_text("2026-10-13T12:00:00+00:00 review PR open\n")
    actual_write = store.write

    def crash(*_args):
        raise RuntimeError("simulated crash before atomic state write")

    monkeypatch.setattr(store, "write", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        pulse(plan, tmp_path, world)
    assert store.read(tmp_path)["items"]["a"]["state"] == "running"
    assert store.read(tmp_path)["items"]["a"].get("report_offset", 0) == 0
    assert coord.sent == []
    monkeypatch.setattr(store, "write", actual_write)
    pulse(plan, tmp_path, world)
    assert store.read(tmp_path)["items"]["a"]["state"] == "review"
    assert len(coord.sent) == 1
    pulse(plan, tmp_path, world)
    assert len(coord.sent) == 1


def test_torn_line_waits_for_completion(tmp_path):
    plan, reports, world, _ = setup(tmp_path)
    reports.write_text("2026-10-13T12:00:00+00:00 running halfway")
    pulse(plan, tmp_path, world)
    assert store.read(tmp_path)["items"]["a"].get("report_offset", 0) == 0
    with reports.open("a") as f:
        f.write(" there\n")
    pulse(plan, tmp_path, world)
    assert store.read(tmp_path)["items"]["a"]["last_line"] == "halfway there"


def test_disappeared_file_is_attention_and_does_not_reset_offset(tmp_path):
    plan, reports, world, _ = setup(tmp_path)
    reports.write_text("2026-10-13T12:00:00+00:00 running started\n")
    pulse(plan, tmp_path, world)
    saved = store.read(tmp_path)["items"]["a"]["report_offset"]
    reports.unlink()
    pulse(plan, tmp_path, world)
    doc = store.read(tmp_path)
    assert doc["items"]["a"]["report_offset"] == saved
    assert any(str(reports) in a for a in render.summary(plan, doc, Config(), T0)["attention"])


def test_replaced_file_at_same_path_reads_its_first_line(tmp_path):
    plan, reports, world, _ = setup(tmp_path)
    reports.write_text("2026-10-13T12:00:00+00:00 running " + "old " * 50 + "\n")
    pulse(plan, tmp_path, world)
    old_offset = store.read(tmp_path)["items"]["a"]["report_offset"]
    reports.unlink()
    reports.write_text("2026-10-13T12:00:00+00:00 review replacement first\n" +
                       "2026-10-13T12:00:00+00:00 running " + "new " * 50 + "\n")
    assert reports.stat().st_size > old_offset
    pulse(plan, tmp_path, world)
    rec = store.read(tmp_path)["items"]["a"]
    assert rec["report_offset"] == reports.stat().st_size
    assert any(e.get("line") == "replacement first" for e in store.events(tmp_path, "a"))


def test_rewritten_file_on_same_inode_reads_from_start(tmp_path):
    plan, reports, world, _ = setup(tmp_path)
    reports.write_text("2026-10-13T12:00:00+00:00 running " + "old " * 50 + "\n")
    pulse(plan, tmp_path, world)
    old_inode = reports.stat().st_ino
    reports.write_text("2026-10-13T12:00:00+00:00 review rewritten first\n" +
                       "2026-10-13T12:00:00+00:00 running " + "new " * 50 + "\n")
    assert reports.stat().st_ino == old_inode
    pulse(plan, tmp_path, world)
    assert any(e.get("line") == "rewritten first" for e in store.events(tmp_path, "a"))


def test_transient_read_error_clears_after_a_successful_read(tmp_path, monkeypatch):
    plan, reports, world, _ = setup(tmp_path)
    reports.write_text("2026-10-13T12:00:00+00:00 running recovered\n")
    actual_read = engine._report_lines
    monkeypatch.setattr(engine, "_report_lines", lambda *args: None)
    pulse(plan, tmp_path, world)
    assert any("cannot read reports" in a for a in
               render.summary(plan, store.read(tmp_path), Config(), T0)["attention"])
    monkeypatch.setattr(engine, "_report_lines", actual_read)
    pulse(plan, tmp_path, world)
    assert store.read(tmp_path)["items"]["a"]["last_line"] == "recovered"
    assert not any("cannot read reports" in a for a in
                   render.summary(plan, store.read(tmp_path), Config(), T0)["attention"])


def test_read_error_does_not_raise_attention_after_item_finishes(tmp_path, monkeypatch):
    plan, reports, world, _ = setup(tmp_path)
    reports.write_text("2026-10-13T12:00:00+00:00 running progress\n")
    monkeypatch.setattr(engine, "_report_lines", lambda *args: None)
    pulse(plan, tmp_path, world)
    assert any("cannot read reports" in a for a in
               render.summary(plan, store.read(tmp_path), Config(), T0)["attention"])
    engine.mark(plan, tmp_path, world, "a", "done", "finished through another channel")
    assert not any("cannot read reports" in a for a in
                   render.summary(plan, store.read(tmp_path), Config(), T0)["attention"])


def test_malformed_line_is_attention_and_does_not_hide_later_reports(tmp_path):
    plan, reports, world, _ = setup(tmp_path)
    reports.write_text("not a report\n2026-10-13T12:00:00+00:00 waiting Daniel must decide\n")
    pulse(plan, tmp_path, world)
    doc = store.read(tmp_path)
    assert doc["items"]["a"]["waiting"] == "Daniel must decide"
    assert any(str(reports) in line for line in render.summary(plan, doc, Config(), T0)["attention"])
    assert doc["items"]["a"]["report_offset"] == reports.stat().st_size


def test_done_from_file_checks_oracle(tmp_path):
    plan, reports, world, _ = setup(tmp_path, "cmd:false")
    reports.write_text("2026-10-13T12:00:00+00:00 done finished\n")
    pulse(plan, tmp_path, world)
    rec = store.read(tmp_path)["items"]["a"]
    assert rec["state"] == "review" and rec["claimed_done"] is True
    assert rec["oracle"]["ok"] is False


def test_inactive_item_file_is_ignored(tmp_path):
    plan, reports, world, coord = setup(tmp_path)
    reports.write_text("2026-10-13T12:00:00+00:00 running from active\n")
    other = tmp_path / "other" / ".zamu"
    other.mkdir(parents=True)
    (other / "reports").write_text("2026-10-13T12:00:00+00:00 running forbidden\n")
    pulse(plan, tmp_path, world)
    assert store.read(tmp_path)["items"]["b"]["state"] == "done"
    assert not coord.sent


def test_prompt_names_both_reporting_paths(tmp_path):
    from zamu.launch import worker_prompt

    plan, reports, _, _ = setup(tmp_path)
    prompt = worker_prompt(plan, plan.items["a"], str(reports.parent.parent), False)
    assert "zamu report --plan" in prompt
    assert str(reports) in prompt
    assert "If the command fails" in prompt
