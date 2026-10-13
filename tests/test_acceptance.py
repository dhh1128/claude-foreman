"""The handoff's acceptance test: a fixture plan drawn from a real cross-repo roadmap, driven
through the real `zamu` command in a fresh process per call, against temp git repos and
fake session-coord / tmux / gh executables.

Each test is one of the seven demonstrations the handoff asked for, named after it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import shlex

from fakes import BIN, World, make_repo

WRAP = shlex.join(["sh", "-c", '"$@"; exec "${SHELL:-/bin/sh}" -l'])

PLAN = '''
[plan]
name = "roadmap"
common_brief = "briefs/common.md"

[items.lib-rel]
title = "lib 0.10.0 released"
kind = "external"
oracle = "file:{sig}/lib-0.10.0"

[items.core-lib]
title = "Bump lib in core"
kind = "build"
repo = "{repos}/core"
depends_on = ["lib-rel"]
oracle = "pr:acme/core#12"
brief = "briefs/core-lib.md"

[items.app-lib]
title = "Bump lib in app"
kind = "build"
repo = "{repos}/app"
depends_on = ["core-lib"]
oracle = "file:{sig}/app-lib"
brief = "briefs/app-lib.md"

[items.ext-lib]
title = "Bump lib in ext"
kind = "build"
repo = "{repos}/ext"
depends_on = ["app-lib"]
oracle = "file:{sig}/ext-lib"

[items.relay-lib]
title = "Bump lib in relay"
kind = "build"
repo = "{repos}/relay"
depends_on = ["lib-rel"]
oracle = "file:{sig}/relay-lib"

[items.app-decisions]
title = "Record the release decisions in the design log"
kind = "build"
repo = "{repos}/app"
oracle = "file:{sig}/decisions"

[items.app-feature]
title = "Feature support"
kind = "build"
repo = "{repos}/app"
depends_on = ["app-decisions"]
oracle = "file:{sig}/app-feature"

[items.upstream-prs]
title = "Listed upstream PRs merged"
kind = "external"
oracle = ["pr:upstream/lib#1576", "pr:upstream/lib#1577"]

[items.app-upstream]
title = "Move app onto the upstream library"
kind = "build"
repo = "{repos}/app"
depends_on = ["upstream-prs"]
oracle = "file:{sig}/app-upstream"

[items.trust-posture]
title = "Trust posture"
kind = "decision"
question = "Which trust posture does the app take for its first public release: pinned roots only, or roots plus delegated agents?"
'''


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    for name in ("core", "app", "ext", "relay"):
        make_repo(tmp_path, name)
    world.zamu("init", "roadmap")
    d = Path(world.env.get("ZAMU_HOME", os.environ["ZAMU_HOME"])) / "plans" / "roadmap"
    (d / "plan.toml").write_text(PLAN.format(sig=world.signals, repos=tmp_path / "repos"))
    for b in ("common", "core-lib", "app-lib"):
        (d / "briefs" / f"{b}.md").write_text(f"brief {b}\n")
    world.plan_dir = d
    world.repos = tmp_path / "repos"
    return world


def board(w, **kw):
    return json.loads(w.zamu("board", "--json", **kw).stdout)


# 1 -------------------------------------------------------------------------------------------
def test_1_ready_set_is_correct_and_updates_when_the_oracle_flips(w):
    b = board(w)
    assert b["launchable"] == ["app-decisions"]
    assert b["states"]["core-lib"] == "blocked"
    assert b["states"]["app-lib"] == "blocked"

    w.zamu("pulse")
    assert board(w)["launchable"] == ["app-decisions"]  # nothing flipped yet

    w.signal("lib-0.10.0")
    out = w.zamu("pulse").stdout
    assert "* lib-rel done" in out
    b = board(w)
    assert b["states"]["lib-rel"] == "done"
    assert b["launchable"] == ["core-lib", "relay-lib", "app-decisions"]
    assert b["states"]["app-lib"] == "blocked"  # still behind core-lib


def test_1b_a_pr_oracle_flips_only_when_every_listed_pr_is_merged(w):
    (w.fake / "merged.txt").write_text("upstream/lib#1576\n")
    w.zamu("pulse")
    assert board(w)["states"]["upstream-prs"] == "ready"
    (w.fake / "merged.txt").write_text("upstream/lib#1576\nupstream/lib#1577\n")
    w.zamu("pulse")
    b = board(w)
    assert b["states"]["upstream-prs"] == "done"
    assert "app-upstream" in b["launchable"]


# 2 -------------------------------------------------------------------------------------------
def test_2_cycles_and_dangling_dependencies_are_refused_at_load(w):
    p = w.plan_dir / "plan.toml"
    good = p.read_text()
    p.write_text(good.replace('depends_on = ["lib-rel"]\noracle = "pr:acme/core#12"',
                              'depends_on = ["lib-rel", "ext-lib"]\noracle = "pr:acme/core#12"'))
    r = w.zamu("check", check=False)
    assert r.returncode == 2
    assert "cycle: app-lib -> core-lib -> ext-lib -> app-lib" in r.stderr
    # Every verb refuses, not only `check`: a PM must not act on a plan it cannot trust.
    assert w.zamu("pulse", check=False).returncode == 2
    assert w.zamu("launch", "--ready", check=False).returncode == 2

    p.write_text(good.replace('depends_on = ["app-decisions"]', 'depends_on = ["decisions"]'))
    r = w.zamu("check", check=False)
    assert r.returncode == 2
    assert "'app-feature' depends on 'decisions', which is not in the plan" in r.stderr

    p.write_text(good)
    assert w.zamu("check").returncode == 0


# 3 -------------------------------------------------------------------------------------------
def test_3_dry_run_prints_the_worktree_and_tmux_command_and_executes_nothing(w):
    w.signal("lib-0.10.0")
    w.zamu("pulse")
    out = w.zamu("launch", "core-lib", env={"TMUX": ""}).stdout
    core = w.repos / "core"
    wt = f"{core}/.worktrees/core-lib"
    assert f"git -C {core} fetch origin main" in out
    assert f"git -C {core} worktree add {wt} -b core-lib origin/main" in out
    line = next(l for l in out.splitlines() if l.startswith("tmux new-window"))
    sid = line.split("--session-id ")[1].split()[0]
    assert f"-n core-lib -c {wt} {WRAP} zamu-worker:{sid} claude --session-id {sid} " in line
    assert "briefs/common.md then " in line and "briefs/core-lib.md" in line
    assert "zamu report --plan roadmap core-lib" in line
    assert "dry run: nothing was executed" in out
    assert not Path(wt).exists()
    assert w.calls("tmux") == []
    assert board(w)["states"]["core-lib"] == "ready"


def test_3b_go_creates_the_worktree_opens_the_window_and_records_the_worker(w):
    out = w.zamu("launch", "app-decisions", "--go", env={"TMUX": ""}).stdout
    wt = w.repos / "app" / ".worktrees" / "app-decisions"
    assert wt.is_dir()
    branch = subprocess.run(["git", "-C", str(wt), "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    assert branch == "app-decisions"
    assert (wt / ".zamu" / ".gitignore").read_text() == "*\n"
    (call,) = w.calls("tmux")
    sid = call[call.index("--session-id") + 1]
    assert "launched app-decisions in pane %100" in out
    rec = json.loads((w.plan_dir / "state.json").read_text())["items"]["app-decisions"]
    assert rec["session"] == sid and rec["pane"] == "%100" and rec["state"] == "running"


# 4 -------------------------------------------------------------------------------------------
def test_4_the_concurrency_cap_holds(w):
    w.signal("lib-0.10.0")
    w.zamu("pulse")
    cap = {"ZAMU_MAX_WORKERS": "2", "TMUX": ""}
    out = w.zamu("launch", "--ready", "--go", env=cap).stdout
    assert out.count("# launched ") == 2
    b = board(w, env=cap)
    assert b["workers"] == [2, 2]
    assert b["slots"] == 0
    assert len(b["launchable"]) == 1  # the third ready item queues

    r = w.zamu("launch", b["launchable"][0], env=cap, check=False)
    assert r.returncode == 2 and "2 of 2 workers are live" in r.stderr
    assert "nothing to launch" in w.zamu("launch", "--ready", env=cap).stdout
    assert len(w.calls("tmux")) == 2

    # A worker that finishes frees its slot.
    first = next(i for i in ("core-lib", "relay-lib", "app-decisions")
                 if b["states"][i] == "running")
    w.zamu("mark", first, "done", "--note", "test")
    assert board(w, env=cap)["slots"] == 1


def test_4b_a_plan_can_lower_the_machine_cap_but_not_raise_it(w):
    p = w.plan_dir / "plan.toml"
    p.write_text(p.read_text().replace('name = "roadmap"', 'name = "roadmap"\nmax_workers = 1'))
    assert board(w)["workers"] == [0, 1]
    p.write_text(p.read_text().replace("max_workers = 1", "max_workers = 9"))
    assert board(w, env={"ZAMU_MAX_WORKERS": "2"})["workers"] == [0, 2]


# 5 -------------------------------------------------------------------------------------------
def test_5_killing_the_pm_mid_run_and_starting_a_new_one_loses_nothing(w):
    w.signal("lib-0.10.0")
    w.zamu("pulse")
    w.zamu("launch", "core-lib", "--go", env={"TMUX": ""})
    w.zamu("report", "core-lib", "review", "PR acme/core#12 open")
    before = board(w)
    state_before = (w.plan_dir / "state.json").read_bytes()

    # A pulse stuck on a slow oracle, killed with SIGKILL partway through -- the reboot case.
    p = w.plan_dir / "plan.toml"
    p.write_text(p.read_text().replace('oracle = "file:{}/ext-lib"'.format(w.signals),
                                       'oracle = "cmd:sleep 30"'))
    proc = subprocess.Popen([sys.executable, str(BIN), "pulse"], env=w.env, cwd=w.tmp,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    time.sleep(1.0)
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()

    # The fresh PM sees exactly what the old one left: same record, same board.
    assert (w.plan_dir / "state.json").read_bytes() == state_before
    after = board(w)
    assert after == before
    assert after["states"]["core-lib"] == "review"
    assert after["states"]["lib-rel"] == "done"

    # And it carries on from there: the merge lands, a new pulse closes the item.
    p.write_text(p.read_text().replace('oracle = "cmd:sleep 30"',
                                       'oracle = "file:{}/ext-lib"'.format(w.signals)))
    (w.fake / "merged.txt").write_text("acme/core#12\n")
    w.zamu("pulse")
    assert board(w)["states"]["core-lib"] == "done"
    assert board(w)["states"]["app-lib"] == "ready"


# 6 -------------------------------------------------------------------------------------------
def test_6_a_worker_report_updates_the_board(w):
    w.zamu("launch", "app-decisions", "--go", env={"TMUX": ""})
    w.zamu("report", "app-decisions", "running", "drafted three of five decision nodes")
    out = w.zamu("board").stdout
    assert 'app-decisions running' in out and "drafted three of five" in out

    w.zamu("report", "app-decisions", "stuck", "need the user's ruling on node 4")
    assert "app-decisions: stuck" in w.zamu("board").stdout

    # "done" is a claim. The oracle has not flipped, so the item is held in review.
    r = w.zamu("report", "app-decisions", "done", "all five committed").stdout
    assert "held in review" in r
    b = board(w)
    assert b["states"]["app-decisions"] == "review"
    assert any("claims done" in a for a in b["attention"])
    assert "app-feature" not in b["launchable"]

    w.signal("decisions")
    w.zamu("report", "app-decisions", "done", "all five committed")
    b = board(w)
    assert b["states"]["app-decisions"] == "done"
    assert "app-feature" in b["launchable"]


def test_6b_reports_wake_an_attached_pm_through_session_coord(w):
    w.zamu("attach", "--repo", str(w.repos / "app"), "--target", "pm-roadmap")
    w.zamu("report", "app-decisions", "running", "milestone one")
    assert not (w.fake / "msgs.txt").exists()  # milestones do not ring
    w.zamu("report", "app-decisions", "review", "PR #3 open")
    (m,) = [json.loads(x) for x in (w.fake / "msgs.txt").read_text().splitlines()]
    assert m[:4] == ["msg", "--repo", str(w.repos / "app"), "pm-roadmap"]
    assert "app-decisions review" in m[4]


# 7 -------------------------------------------------------------------------------------------
def test_7_a_decision_item_produces_exactly_one_ask_and_never_a_launch(w):
    w.zamu("pulse")
    w.zamu("pulse")
    w.zamu("pulse")  # three fresh processes: three PM wakes, or a PM restarted twice
    asks = [c for c in w.calls("session-coord") if c[:2] == ["ask", "add"]]
    assert len(asks) == 1
    a = asks[0]
    assert a[a.index("--kind") + 1] == "Q"
    text = a[a.index("--text") + 1]
    assert "Trust posture" in text and "pinned roots only" in text
    b = board(w)
    assert b["states"]["trust-posture"] == "ready"
    assert any(x.startswith("trust-posture (decision, asked Q-") for x in b["waiting"])

    r = w.zamu("launch", "trust-posture", check=False)
    assert r.returncode == 2 and "decision item" in r.stderr
    assert "trust-posture" not in b["launchable"]
    w.zamu("launch", "--ready", "--go", env={"TMUX": ""})
    assert all("trust-posture" not in json.dumps(c) for c in w.calls("tmux"))


def test_7b_losing_the_state_record_does_not_file_twice(w):
    """If state.json is lost after an ask was filed, the stable tag makes the refile a no-op."""
    w.zamu("pulse")
    (w.plan_dir / "state.json").unlink()
    out = w.zamu("pulse").stdout
    assert "escalated" in out
    assert len(w.asks()) == 1
    assert "was already filed" in (w.plan_dir / "events.jsonl").read_text()


# Beyond the seven -----------------------------------------------------------------------------
def test_dead_worker_is_detected_from_session_coord_and_can_be_relaunched(w):
    cfg = Path(os.environ["ZAMU_CONFIG"])
    cfg.write_text("launch_grace_minutes = 0\n")
    w.zamu("launch", "app-decisions", "--go", env={"TMUX": ""})
    sid = json.loads((w.plan_dir / "state.json").read_text())["items"]["app-decisions"]["session"]

    w.set_board([sid])
    w.zamu("pulse")
    assert "app-decisions running live" in w.zamu("board").stdout

    w.set_board([])
    out = w.zamu("pulse").stdout
    assert "app-decisions worker gone" in out
    assert board(w)["workers"] == [0, 3]  # a dead worker holds no slot

    (w.fake / "board.json").write_text("not json")  # session-coord could not be read
    (w.fake / "tmux-list-fails").touch()  # tmux could not be read either
    w.zamu("pulse")
    assert "running unknown" in w.zamu("board").stdout

    (w.fake / "tmux-list-fails").unlink()
    w.set_board([])
    w.zamu("pulse")
    out = w.zamu("launch", "--relaunch", "app-decisions", "--go", env={"TMUX": ""}).stdout
    call = w.calls("tmux")[-1]
    assert call[call.index("--resume") + 1] == sid
    assert "git -C" not in out.split("# launched")[0].split("\n", 1)[1]  # worktree reused


def test_a_relaunch_of_a_live_worker_is_refused(w):
    w.zamu("launch", "app-decisions", "--go", env={"TMUX": ""})
    r = w.zamu("launch", "--relaunch", "app-decisions", check=False)
    assert r.returncode == 2 and "second worker" in r.stderr


def test_merge_contention_is_on_the_board(w):
    w.zamu("adopt", "app-decisions", "--session", "s1")
    w.zamu("report", "app-decisions", "review", "PR #1")
    assert "merges queue" not in w.zamu("board").stdout
    (w.fake / "merged.txt").write_text("upstream/lib#1576\nupstream/lib#1577\n")
    w.zamu("pulse")
    w.zamu("adopt", "app-upstream", "--session", "s2")
    w.zamu("report", "app-upstream", "review", "PR #2")
    out = w.zamu("board").stdout
    assert f"merges queue in {w.repos / 'app'}: app-decisions, app-upstream" in out


def test_deadlines_flag_a_chain_that_cannot_finish(w):
    p = w.plan_dir / "plan.toml"
    t = p.read_text().replace('name = "roadmap"', 'name = "roadmap"\ndue = 2000-01-01')
    p.write_text(t)
    out = w.zamu("board").stdout
    assert "ext-lib: overdue (due 2000-01-01" in out


def test_a_worker_started_by_hand_can_be_adopted(w):
    """The first real plan began with one worker the human had already started himself."""
    core = w.repos / "core"
    w.set_board(["hand-started-1"])
    out = w.zamu("adopt", "app-decisions", "--session", "hand-started-1", "--pane", "%7",
                    "--worktree", str(w.repos / "app")).stdout
    assert "adopted" in out
    w.zamu("pulse")
    b = board(w)
    assert b["states"]["app-decisions"] == "running"
    assert b["workers"] == [1, 3]
    assert "app-decisions running live" in w.zamu("board").stdout
    assert not [c for c in w.calls("tmux") if c[0] == "new-window"]  # adoption launches nothing

    # Its reports count like any worker's.
    w.zamu("report", "app-decisions", "review", "PR #3 open")
    assert board(w)["states"]["app-decisions"] == "review"

    # Refused: an item that is not a build item, or one that already has a live worker.
    r = w.zamu("adopt", "trust-posture", "--session", "x", check=False)
    assert r.returncode == 2 and "decision item" in r.stderr
    r = w.zamu("adopt", "app-decisions", "--session", "other", check=False)
    assert r.returncode == 2 and "already has a worker" in r.stderr


def test_relaunching_an_adopted_worker_reuses_its_recorded_tree(w):
    cfg = Path(os.environ["ZAMU_CONFIG"])
    cfg.write_text("launch_grace_minutes = 0\n")
    app = w.repos / "app"
    w.zamu("adopt", "app-decisions", "--session", "hand-1", "--worktree", str(app))
    w.set_board([])
    w.zamu("pulse")
    out = w.zamu("launch", "--relaunch", "app-decisions").stdout
    assert "worktree add" not in out
    line = next(l for l in out.splitlines() if l.startswith("tmux new-window"))
    assert f"-c {app} {WRAP} zamu-worker:hand-1 claude --resume hand-1" in line


def test_launch_runs_claude_under_a_shell_so_session_coord_can_find_it(w):
    """session-coord looks for a pane's claude among the pane process's descendants."""
    w.zamu("launch", "app-decisions", "--go", env={"TMUX": ""})
    (call,) = w.calls("tmux")
    i = call.index("-c")
    assert call[i + 2:i + 5] == ["sh", "-c", '"$@"; exec "${SHELL:-/bin/sh}" -l']
    assert call[i + 5] == "zamu-worker:" + call[call.index("--session-id") + 1]
    assert call[i + 6] == "claude"
    prompt = call[-1]
    assert "do not stop to ask the user" in prompt


def test_configured_claude_flags_reach_the_worker(w):
    Path(os.environ["ZAMU_CONFIG"]).write_text(
        'claude = ["claude", "--dangerously-skip-permissions"]\n')
    out = w.zamu("launch", "app-decisions").stdout
    assert "claude --dangerously-skip-permissions --session-id " in out


def test_the_pm_reporting_on_a_workers_behalf_does_not_ring_itself(w):
    w.zamu("attach", "--repo", str(w.repos / "app"), "--target", "pm", env={"TMUX_PANE": "%9"})
    w.zamu("report", "app-decisions", "stuck", "waiting on Q-1", env={"TMUX_PANE": "%9"})
    assert not (w.fake / "msgs.txt").exists()
    w.zamu("report", "app-decisions", "stuck", "waiting on Q-1", env={"TMUX_PANE": "%3"})
    assert (w.fake / "msgs.txt").exists()


def test_adopt_warns_when_the_tree_does_not_exist(w):
    out = w.zamu("adopt", "app-decisions", "--session", "s1").stdout
    assert "does not exist yet" in out


def test_a_worker_waiting_on_the_user_is_on_the_board_until_it_resumes(w):
    w.zamu("adopt", "app-decisions", "--session", "s1")
    w.zamu("report", "app-decisions", "waiting", "decision node @7eg285mu awaits approval (Q-ABCD)")
    b = board(w)
    assert any("app-decisions: waiting on the user" in a and "@7eg285mu" in a for a in b["attention"])
    w.zamu("report", "app-decisions", "running", "approved; implementing")
    assert not any("waiting" in a for a in board(w)["attention"])


# R-EPTZ: pins, staleness, verify, reopen, evidence -------------------------------------------
def _with_pin(w, freeze="2099-01-01"):
    (w.signals / "core.pin").write_text("75de363\n")
    p = w.plan_dir / "plan.toml"
    t = p.read_text().replace('name = "roadmap"', f'name = "roadmap"\nfreeze = {freeze}')
    t = t.replace('[items.app-decisions]\n', '[items.app-decisions]\nrests_on = ["core"]\n')
    t += f'\n[pins.core]\ncmd = "cat {w.signals}/core.pin"\n'
    p.write_text(t)


def test_a_moved_pin_makes_resting_evidence_stale_and_verify_reopens_or_reaffirms(w):
    _with_pin(w)
    w.signal("decisions")
    w.zamu("pulse")  # baseline pin, and app-decisions closes on its oracle
    b = board(w)
    assert b["states"]["app-decisions"] == "done"
    assert not any("pin" in c for c in b["attention"])

    (w.signals / "core.pin").write_text("9abc111\n")
    out = w.zamu("pulse").stdout
    assert "* pin core moved 75de363 -> 9abc111; evidence now stale for app-decisions" in out
    assert "AFTER ITS FREEZE" not in out
    assert any("app-decisions: evidence stale" in a for a in board(w)["attention"])

    # A falsifier run elsewhere (say, ten cold starts) passed: record it, with where it lives.
    r = w.zamu("verify", "app-decisions", "--ok", "evidence/M5/runs-10-9abc111").stdout
    assert "verified at pins {'core': '9abc111'}" in r
    assert not any("stale" in a for a in board(w)["attention"])
    assert "evidence/M5/runs-10-9abc111 at core=9abc111" in w.zamu("show", "app-decisions").stdout

    # The next move breaks it: reopen. The oracle alone does not close it again.
    (w.signals / "core.pin").write_text("deadbee\n")
    w.zamu("pulse")
    w.zamu("verify", "app-decisions", "--fail", "act 4b red on deadbee")
    w.zamu("pulse")
    b = board(w)
    assert b["states"]["app-decisions"] == "ready"
    assert any("app-decisions: reopened" in a for a in b["attention"])
    w.zamu("adopt", "app-decisions", "--session", "s9")
    w.zamu("report", "app-decisions", "done", "fixed for deadbee", "--evidence", "PR #7")
    assert board(w)["states"]["app-decisions"] == "done"


def test_a_pin_moving_after_its_freeze_is_called_out(w):
    _with_pin(w, freeze="2000-01-01")
    w.zamu("pulse")
    (w.signals / "core.pin").write_text("9abc111\n")
    assert "AFTER ITS FREEZE (2000-01-01)" in w.zamu("pulse").stdout


def test_an_unreadable_pin_is_not_a_move(w):
    _with_pin(w)
    w.zamu("pulse")
    (w.signals / "core.pin").unlink()
    assert "pin core" not in w.zamu("pulse").stdout
    (w.signals / "core.pin").write_text("75de363\n")
    assert "pin core" not in w.zamu("pulse").stdout


# R-YJY0: workers separate from items ---------------------------------------------------------
def test_one_worker_carries_items_in_turn_and_holds_one_slot(w):
    w.zamu("worker", "add", "sub-a", "--engine", "subagent", "--note", "PM's re-pin subagent")
    out = w.zamu("assign", "app-decisions", "sub-a").stdout
    assert "app-decisions assigned to sub-a (subagent)" in out
    assert "Deliver this brief" in out and "zamu report --plan roadmap app-decisions" in out
    (w.fake / "merged.txt").write_text("upstream/lib#1576\nupstream/lib#1577\n")
    w.zamu("pulse")
    w.zamu("assign", "app-upstream", "sub-a")
    w.zamu("pulse")
    b = board(w)
    assert b["workers"] == [1, 3]
    assert "app-upstream [sub-a] running unknown" in w.zamu("board").stdout

    w.zamu("mark", "app-decisions", "done")
    w.zamu("mark", "app-upstream", "done")
    assert "idle workers, assignable: sub-a (subagent)" in w.zamu("board").stdout
    assert "sub-a  subagent" in w.zamu("worker", "list").stdout


def test_worker_retire_command_removes_a_finished_workers_tree_and_keeps_its_branch(w):
    repo = w.repos / "app"
    tree = repo / ".worktrees" / "spare"
    tree.parent.mkdir(exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "spare", str(tree)],
                   check=True)
    w.zamu("worker", "add", "spare", "--engine", "subagent", "--worktree", str(tree))
    w.zamu("assign", "app-decisions", "spare")
    w.zamu("mark", "app-decisions", "done")
    out = w.zamu("worker", "retire", "spare").stdout
    assert "spare retired" in out
    assert not tree.exists()
    assert "spare" in subprocess.run(["git", "-C", str(repo), "branch", "--list", "spare"],
                                     check=True, capture_output=True, text=True).stdout
    assert "spare (subagent)" not in w.zamu("board").stdout


def test_a_codex_item_launches_codex_with_the_prompt_and_no_session_flag(w):
    Path(os.environ["ZAMU_CONFIG"]).write_text('[engines]\ncodex = ["codex", "--yolo"]\n')
    p = w.plan_dir / "plan.toml"
    p.write_text(p.read_text().replace('[items.app-decisions]\n',
                                       '[items.app-decisions]\nengine = "codex"\n'))
    out = w.zamu("launch", "app-decisions").stdout
    line = next(l for l in out.splitlines() if l.startswith("tmux new-window"))
    assert " codex --yolo 'You are the worker session" in line
    assert "--session-id" not in line and " zamu-worker:" in line


def test_an_item_naming_an_unknown_engine_is_refused_at_launch(w):
    p = w.plan_dir / "plan.toml"
    p.write_text(p.read_text().replace('[items.app-decisions]\n',
                                       '[items.app-decisions]\nengine = "gemini"\n'))
    r = w.zamu("launch", "app-decisions", check=False)
    assert r.returncode == 2 and "engine 'gemini'" in r.stderr


# Q-GCVF: standing rules the PM applies; documents it owns; a merge moves others' base -------
def test_rules_are_recorded_on_disk_and_owned_documents_listed(w):
    p = w.plan_dir / "plan.toml"
    p.write_text(p.read_text().replace('name = "roadmap"',
                                       'name = "roadmap"\nowns = ["status.md"]'))
    assert "(none yet)" in w.zamu("rules").stdout
    w.zamu("rules", "--add", "Hand-merge when the gate objects only on scope; "
              "cross-engine review required.", "--source", "Q-MMQY")
    out = w.zamu("rules").stdout
    assert "(Q-MMQY): Hand-merge when the gate objects only on scope" in out
    assert f"documents the PM maintains: {w.plan_dir / 'status.md'}" in out


def test_landing_an_item_tells_the_pm_whose_base_moved(w):
    w.zamu("adopt", "app-decisions", "--session", "s1")
    (w.fake / "merged.txt").write_text("upstream/lib#1576\nupstream/lib#1577\n")
    w.zamu("pulse")
    w.zamu("adopt", "app-upstream", "--session", "s2")
    out = w.zamu("mark", "app-decisions", "done").stdout
    assert f"base moved for app-upstream in {w.repos / 'app'}" in out


def test_the_cap_is_the_machines_and_counts_workers_in_every_plan(w):
    """foreman-imbu's catch: plan B said 0/4 while plan A had 4 live."""
    w.zamu("init", "second")
    d2 = Path(os.environ["ZAMU_HOME"]) / "plans" / "second"
    (d2 / "plan.toml").write_text(
        f'[items.b1]\ntitle="B1"\nkind="build"\nrepo="{w.repos / "relay"}"\noracle="manual"\n')
    cap = {"ZAMU_MAX_WORKERS": "2", "TMUX": ""}
    w.zamu("adopt", "--plan", "roadmap", "app-decisions", "--session", "s1", env=cap)
    w.zamu("adopt", "--plan", "roadmap", "app-upstream", "--session", "s2", env=cap)
    b = json.loads(w.zamu("board", "--plan", "second", "--json", env=cap).stdout)
    assert b["workers"] == [2, 2] and b["slots"] == 0 and b["elsewhere"] == {"roadmap": 2}
    assert "workers 2/2 (2 in roadmap 2)" in w.zamu("board", "--plan", "second", env=cap).stdout
    r = w.zamu("launch", "--plan", "second", "b1", env=cap, check=False)
    assert r.returncode == 2 and "2 of 2 workers are live" in r.stderr
    w.zamu("mark", "--plan", "roadmap", "app-upstream", "done", env=cap)
    assert json.loads(w.zamu("board", "--plan", "second", "--json", env=cap).stdout)["slots"] == 1


def test_an_engine_cap_holds_across_plans_and_other_engines_still_launch(w):
    Path(os.environ["ZAMU_CONFIG"]).write_text(
        '[engine_caps]\nclaude = 1\n[engines]\ncodex = ["codex"]\n')
    w.zamu("adopt", "app-decisions", "--session", "s1")  # one live claude worker
    p = w.plan_dir / "plan.toml"
    w.signal("lib-0.10.0")
    w.zamu("pulse")
    r = w.zamu("launch", "core-lib", check=False)
    assert r.returncode == 2 and "1 claude worker(s) are live, its cap" in r.stderr
    p.write_text(p.read_text().replace('[items.core-lib]\n', '[items.core-lib]\nengine = "codex"\n'))
    assert " codex 'You are the worker session" in w.zamu("launch", "core-lib").stdout
    # --ready skips claude items while claude is at its cap, and still offers codex ones.
    out = w.zamu("launch", "--ready").stdout
    assert "core-lib" in out and "relay-lib" not in out
