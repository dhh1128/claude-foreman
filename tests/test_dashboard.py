"""Human dashboard over the portfolio's one cross-plan read."""

import datetime as dt
import json
import subprocess

from zamu import config, portfolio
from zamu.config import Config
from zamu.engine import World
from fakes import World as FakeWorld, git, make_repo

NOW = dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc)


def test_dashboard_scopes_detail_but_counts_other_plans(tmp_path):
    root = config.plans_dir()
    for name in ("alpha", "beta"):
        d = root / name
        d.mkdir(parents=True)
        (d / "plan.toml").write_text('[items.decide]\nkind="decision"\ntitle="Choose"\nquestion="Ship?"\n')
        (d / "state.json").write_text(json.dumps({"items": {"decide": {"ask": "Q-ONE"}}, "pm": {"tmux_session": "me"}}))
    world = World(now=lambda: NOW, scan=lambda: [], run=lambda argv: (0, "active", ""))
    data = portfolio.snapshot(Config(), world, dashboard=True, claude_projects=tmp_path / "none", codex_sessions=tmp_path / "none")
    out = portfolio.dashboard(data, "alpha")
    assert "alpha/decide Q-ONE" in out and "me:alpha-prj" in out
    assert "beta: 1 ready; PM ?; 1 waiting on Daniel" in out
    assert "beta/decide Q-ONE" not in out
    assert "beta/decide Q-ONE" in portfolio.dashboard(data, None)


def test_dashboard_shows_worker_git_pr_ci_and_named_window(tmp_path):
    root = config.plans_dir() / "alpha"
    root.mkdir(parents=True)
    repo = tmp_path / "repo"
    wt = repo / ".worktrees" / "work"
    wt.mkdir(parents=True)
    (root / "plan.toml").write_text(f'[items.work]\nkind="build"\ntitle="Work"\nrepo="{repo}"\noracle="manual"\n')
    state = {"items": {"work": {"state": "running", "wid": "work", "engine": "codex", "worker": "live", "pane": "%7", "worktree": str(wt), "launched": "2026-10-14T09:00:00+00:00", "last_report": "2026-10-14T11:00:00+00:00", "last_line": "Tests passing", "waiting": "Approve scope", "ask": "Q-SCOPE"}}, "pm": {"tmux_session": "me"}}
    (root / "state.json").write_text(json.dumps(state))
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[:2] == ["git", "-C"]:
            if "remote" in argv:
                return 0, "git@github.com:owner/repo.git\n", ""
            if "symbolic-ref" in argv:
                return 0, "origin/main\n", ""
            if "merge-base" in argv:
                return 0, "abc123\n", ""
            if "rev-list" in argv:
                return 0, "3\n", ""
            if "status" in argv:
                return 0, " M a.py\n?? b.py\n", ""
        if argv[:2] == ["gh", "pr"]:
            return 0, json.dumps([{"url": "https://github.com/owner/repo/pull/7", "state": "OPEN", "statusCheckRollup": [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]}]), ""
        if argv[:2] == ["tmux", "display-message"]:
            return 0, "me:work\n", ""
        return 0, "active", ""

    data = portfolio.snapshot(Config(engines={"codex": ("codex", "-m", "gpt-6")}), World(now=lambda: NOW, scan=lambda: [], run=run), dashboard=True, claude_projects=tmp_path / "none", codex_sessions=tmp_path / "none")
    out = portfolio.dashboard(data, "alpha")
    assert "alpha/work" in out and "codex/gpt-6" in out and "me:work" in out
    assert "3h" in out and "reported 2026-10-14T11:00" in out and "Tests passing" in out
    assert "3 commits" in out and "2 files" in out
    assert "https://github.com/owner/repo/pull/7" in out and "CI pass" in out
    assert "alpha/work Q-SCOPE" in out and "me:work" in out
    assert any(argv[:2] == ["gh", "pr"] for argv in calls)


def test_dashboard_late_chain_and_landed_today(tmp_path):
    d = config.plans_dir() / "alpha"
    d.mkdir(parents=True)
    (d / "plan.toml").write_text('[items.a]\nkind="external"\ntitle="A"\noracle="manual"\nestimate="1d"\n[items.b]\nkind="external"\ntitle="B"\noracle="manual"\ndepends_on=["a"]\ndue=2026-10-12\nestimate="2d"\n[items.done]\nkind="external"\ntitle="Done"\noracle="pr:owner/repo#9"\n')
    (d / "state.json").write_text(json.dumps({"items": {"done": {"state": "done"}}, "pm": {}}))
    (d / "events.jsonl").write_text(json.dumps({"t": "2026-10-14T10:00:00+00:00", "item": "done", "what": "oracle", "ok": True}) + "\n")
    data = portfolio.snapshot(Config(), World(now=lambda: NOW, scan=lambda: [], run=lambda argv: (0, "active", "")), dashboard=True, claude_projects=tmp_path / "none", codex_sessions=tmp_path / "none")
    out = portfolio.dashboard(data, "alpha")
    assert "alpha/b" in out and "5d" in out and "a → b" in out
    assert "alpha/done" in out and "https://github.com/owner/repo/pull/9" in out


def test_dashboard_marks_unreadable_progress_unknown(tmp_path):
    d = config.plans_dir() / "alpha"
    d.mkdir(parents=True)
    (d / "plan.toml").write_text('[items.work]\nkind="build"\ntitle="Work"\nrepo="/missing"\noracle="manual"\n')
    (d / "state.json").write_text(json.dumps({"items": {"work": {"state": "running", "wid": "w", "worker": "live", "pane": "%9", "launched": NOW.isoformat()}}, "pm": {"tmux_session": "me"}}))
    data = portfolio.snapshot(Config(), World(now=lambda: NOW, scan=lambda: [], run=lambda argv: (1, "", "no repo")), dashboard=True, claude_projects=tmp_path / "none", codex_sessions=tmp_path / "none")
    out = portfolio.dashboard(data, "alpha")
    assert "commits ?" in out and "files ?" in out and "PR ?" in out


def test_dashboard_reads_actual_model_from_claude_log(tmp_path):
    d = config.plans_dir() / "alpha"
    d.mkdir(parents=True)
    (d / "plan.toml").write_text('[items.work]\nkind="build"\ntitle="Work"\nrepo="/missing"\noracle="manual"\n')
    (d / "state.json").write_text(json.dumps({"items": {"work": {"state": "running", "wid": "w", "engine": "claude", "worker": "live", "session": "sid", "launched": NOW.isoformat()}}, "pm": {"tmux_session": "me"}}))
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "sid.jsonl").write_text(json.dumps({"message": {"model": "claude-sonnet-4", "id": "m1", "usage": {"input_tokens": 1}}}) + "\n")
    data = portfolio.snapshot(Config(), World(now=lambda: NOW, scan=lambda: [], run=lambda argv: (1, "", "")), dashboard=True, claude_projects=projects, codex_sessions=tmp_path / "none")
    assert "claude/claude-sonnet-4" in portfolio.dashboard(data, "alpha")


def test_dashboard_command_uses_plan_resolution_and_all(tmp_path):
    root = config.plans_dir()
    for name in ("alpha", "beta"):
        d = root / name
        d.mkdir(parents=True)
        (d / "plan.toml").write_text('[items.decide]\nkind="decision"\ntitle="Choose"\nquestion="Ship?"\n')
        (d / "state.json").write_text(json.dumps({"items": {"decide": {"ask": "Q-ONE"}}, "pm": {"tmux_session": "me"}}))
    fake = FakeWorld(tmp_path)
    scoped = fake.zamu("dashboard", "--plan", "alpha").stdout
    assert "alpha/decide" in scoped and "beta/decide" not in scoped
    assert "alpha/decide" in fake.zamu("dashboard", "--all").stdout
    assert "beta/decide" in fake.zamu("dashboard", "--all").stdout


def test_dashboard_progress_against_real_git_and_fake_gh(tmp_path):
    repo = make_repo(tmp_path, "repo")
    wt = repo / ".worktrees" / "work"
    git("worktree", "add", "-q", "-b", "work", str(wt), "origin/main", cwd=repo)
    (wt / "committed.txt").write_text("one")
    git("add", "committed.txt", cwd=wt)
    git("commit", "-q", "-m", "one", cwd=wt)
    (wt / "dirty.txt").write_text("two")
    git("remote", "set-url", "origin", "git@github.com:owner/repo.git", cwd=repo)
    d = config.plans_dir() / "alpha"
    d.mkdir(parents=True)
    (d / "plan.toml").write_text(f'[items.work]\nkind="build"\ntitle="Work"\nrepo="{repo}"\noracle="manual"\n')
    (d / "state.json").write_text(json.dumps({"items": {"work": {"state": "running", "wid": "work", "worker": "live", "worktree": str(wt)}}, "pm": {"tmux_session": "me"}}))

    def run(argv):
        if argv[0] == "gh":
            assert argv[argv.index("--repo") + 1] == "owner/repo"
            return 0, "[]", ""
        result = subprocess.run(argv, text=True, capture_output=True)
        return result.returncode, result.stdout, result.stderr

    data = portfolio.snapshot(Config(), World(now=lambda: NOW, scan=lambda: [], run=run), dashboard=True, claude_projects=tmp_path / "none", codex_sessions=tmp_path / "none")
    out = portfolio.dashboard(data, "alpha")
    assert "1 commit, 1 file; PR none" in out


def test_dashboard_does_not_call_unreadable_events_empty(tmp_path):
    d = config.plans_dir() / "alpha"
    d.mkdir(parents=True)
    (d / "plan.toml").write_text('[items.done]\nkind="decision"\ntitle="Done"\n')
    (d / "state.json").write_text(json.dumps({"items": {"done": {"state": "done"}}, "pm": {}}))
    (d / "events.jsonl").write_text("bad\n" + json.dumps({"t": NOW.isoformat(), "item": "done", "what": "mark", "state": "done"}) + "\n")
    data = portfolio.snapshot(Config(), World(now=lambda: NOW, scan=lambda: [], run=lambda argv: (0, "active", "")), dashboard=True, claude_projects=tmp_path / "none", codex_sessions=tmp_path / "none")
    assert "alpha: completion events unreadable" in portfolio.dashboard(data, "alpha")


def test_dashboard_uses_actual_tmux_window_name_for_adopted_worker(tmp_path):
    d = config.plans_dir() / "alpha"
    d.mkdir(parents=True)
    (d / "plan.toml").write_text('[items.work]\nkind="build"\ntitle="Work"\nrepo="/missing"\noracle="manual"\n')
    (d / "state.json").write_text(json.dumps({"items": {"work": {"state": "running", "wid": "adopted", "worker": "live", "pane": "%42", "waiting": "Choose scope", "ask": "Q-SCOPE"}}, "pm": {"tmux_session": "pm-session"}}))

    def run(argv):
        if argv[:2] == ["tmux", "display-message"]:
            assert "%42" in argv
            return 0, "daniel:custom-worker\n", ""
        return 1, "", "unavailable"

    data = portfolio.snapshot(Config(), World(now=lambda: NOW, scan=lambda: [], run=run), dashboard=True, claude_projects=tmp_path / "none", codex_sessions=tmp_path / "none")
    out = portfolio.dashboard(data, "alpha")
    assert "Q-SCOPE (+0) Choose scope — daniel:custom-worker" in out
    assert "alpha/work claude/? daniel:custom-worker" in out
