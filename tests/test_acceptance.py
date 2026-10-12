"""The handoff's acceptance test: a fixture plan drawn from a real cross-repo roadmap, driven
through the real `foreman` command in a fresh process per call, against temp git repos and
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

from fakes import BIN, World, make_repo

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
    world.foreman("init", "roadmap")
    d = Path(world.env.get("FOREMAN_HOME", os.environ["FOREMAN_HOME"])) / "plans" / "roadmap"
    (d / "plan.toml").write_text(PLAN.format(sig=world.signals, repos=tmp_path / "repos"))
    for b in ("common", "core-lib", "app-lib"):
        (d / "briefs" / f"{b}.md").write_text(f"brief {b}\n")
    world.plan_dir = d
    world.repos = tmp_path / "repos"
    return world


def board(w, **kw):
    return json.loads(w.foreman("board", "--json", **kw).stdout)


# 1 -------------------------------------------------------------------------------------------
def test_1_ready_set_is_correct_and_updates_when_the_oracle_flips(w):
    b = board(w)
    assert b["launchable"] == ["app-decisions"]
    assert b["states"]["core-lib"] == "blocked"
    assert b["states"]["app-lib"] == "blocked"

    w.foreman("pulse")
    assert board(w)["launchable"] == ["app-decisions"]  # nothing flipped yet

    w.signal("lib-0.10.0")
    out = w.foreman("pulse").stdout
    assert "* lib-rel done" in out
    b = board(w)
    assert b["states"]["lib-rel"] == "done"
    assert b["launchable"] == ["core-lib", "relay-lib", "app-decisions"]
    assert b["states"]["app-lib"] == "blocked"  # still behind core-lib


def test_1b_a_pr_oracle_flips_only_when_every_listed_pr_is_merged(w):
    (w.fake / "merged.txt").write_text("upstream/lib#1576\n")
    w.foreman("pulse")
    assert board(w)["states"]["upstream-prs"] == "ready"
    (w.fake / "merged.txt").write_text("upstream/lib#1576\nupstream/lib#1577\n")
    w.foreman("pulse")
    b = board(w)
    assert b["states"]["upstream-prs"] == "done"
    assert "app-upstream" in b["launchable"]


# 2 -------------------------------------------------------------------------------------------
def test_2_cycles_and_dangling_dependencies_are_refused_at_load(w):
    p = w.plan_dir / "plan.toml"
    good = p.read_text()
    p.write_text(good.replace('depends_on = ["lib-rel"]\noracle = "pr:acme/core#12"',
                              'depends_on = ["lib-rel", "ext-lib"]\noracle = "pr:acme/core#12"'))
    r = w.foreman("check", check=False)
    assert r.returncode == 2
    assert "cycle: app-lib -> core-lib -> ext-lib -> app-lib" in r.stderr
    # Every verb refuses, not only `check`: a PM must not act on a plan it cannot trust.
    assert w.foreman("pulse", check=False).returncode == 2
    assert w.foreman("launch", "--ready", check=False).returncode == 2

    p.write_text(good.replace('depends_on = ["app-decisions"]', 'depends_on = ["decisions"]'))
    r = w.foreman("check", check=False)
    assert r.returncode == 2
    assert "'app-feature' depends on 'decisions', which is not in the plan" in r.stderr

    p.write_text(good)
    assert w.foreman("check").returncode == 0


# 3 -------------------------------------------------------------------------------------------
def test_3_dry_run_prints_the_worktree_and_tmux_command_and_executes_nothing(w):
    w.signal("lib-0.10.0")
    w.foreman("pulse")
    out = w.foreman("launch", "core-lib", env={"TMUX": ""}).stdout
    core = w.repos / "core"
    wt = f"{core}/.worktrees/core-lib"
    assert f"git -C {core} fetch origin main" in out
    assert f"git -C {core} worktree add {wt} -b core-lib origin/main" in out
    line = next(l for l in out.splitlines() if l.startswith("tmux new-window"))
    assert f"-n core-lib -c {wt} claude --session-id " in line
    assert "briefs/common.md then " in line and "briefs/core-lib.md" in line
    assert "foreman report --plan roadmap core-lib" in line
    assert "dry run: nothing was executed" in out
    assert not Path(wt).exists()
    assert w.calls("tmux") == []
    assert board(w)["states"]["core-lib"] == "ready"


def test_3b_go_creates_the_worktree_opens_the_window_and_records_the_worker(w):
    out = w.foreman("launch", "app-decisions", "--go", env={"TMUX": ""}).stdout
    wt = w.repos / "app" / ".worktrees" / "app-decisions"
    assert wt.is_dir()
    branch = subprocess.run(["git", "-C", str(wt), "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    assert branch == "app-decisions"
    (call,) = w.calls("tmux")
    sid = call[call.index("--session-id") + 1]
    assert "launched app-decisions in pane %100" in out
    rec = json.loads((w.plan_dir / "state.json").read_text())["items"]["app-decisions"]
    assert rec["session"] == sid and rec["pane"] == "%100" and rec["state"] == "running"


# 4 -------------------------------------------------------------------------------------------
def test_4_the_concurrency_cap_holds(w):
    w.signal("lib-0.10.0")
    w.foreman("pulse")
    cap = {"FOREMAN_MAX_WORKERS": "2", "TMUX": ""}
    out = w.foreman("launch", "--ready", "--go", env=cap).stdout
    assert out.count("# launched ") == 2
    b = board(w, env=cap)
    assert b["workers"] == [2, 2]
    assert b["slots"] == 0
    assert len(b["launchable"]) == 1  # the third ready item queues

    r = w.foreman("launch", b["launchable"][0], env=cap, check=False)
    assert r.returncode == 2 and "2 of 2 workers are live" in r.stderr
    assert "nothing to launch" in w.foreman("launch", "--ready", env=cap).stdout
    assert len(w.calls("tmux")) == 2

    # A worker that finishes frees its slot.
    first = next(i for i in ("core-lib", "relay-lib", "app-decisions")
                 if b["states"][i] == "running")
    w.foreman("mark", first, "done", "--note", "test")
    assert board(w, env=cap)["slots"] == 1


def test_4b_a_plan_can_lower_the_machine_cap_but_not_raise_it(w):
    p = w.plan_dir / "plan.toml"
    p.write_text(p.read_text().replace('name = "roadmap"', 'name = "roadmap"\nmax_workers = 1'))
    assert board(w)["workers"] == [0, 1]
    p.write_text(p.read_text().replace("max_workers = 1", "max_workers = 9"))
    assert board(w, env={"FOREMAN_MAX_WORKERS": "2"})["workers"] == [0, 2]


# 5 -------------------------------------------------------------------------------------------
def test_5_killing_the_pm_mid_run_and_starting_a_new_one_loses_nothing(w):
    w.signal("lib-0.10.0")
    w.foreman("pulse")
    w.foreman("launch", "core-lib", "--go", env={"TMUX": ""})
    w.foreman("report", "core-lib", "review", "PR acme/core#12 open")
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
    w.foreman("pulse")
    assert board(w)["states"]["core-lib"] == "done"
    assert board(w)["states"]["app-lib"] == "ready"


# 6 -------------------------------------------------------------------------------------------
def test_6_a_worker_report_updates_the_board(w):
    w.foreman("launch", "app-decisions", "--go", env={"TMUX": ""})
    w.foreman("report", "app-decisions", "running", "drafted three of five decision nodes")
    out = w.foreman("board").stdout
    assert 'app-decisions running' in out and "drafted three of five" in out

    w.foreman("report", "app-decisions", "stuck", "need the user's ruling on node 4")
    assert "app-decisions: stuck" in w.foreman("board").stdout

    # "done" is a claim. The oracle has not flipped, so the item is held in review.
    r = w.foreman("report", "app-decisions", "done", "all five committed").stdout
    assert "held in review" in r
    b = board(w)
    assert b["states"]["app-decisions"] == "review"
    assert any("claims done" in a for a in b["attention"])
    assert "app-feature" not in b["launchable"]

    w.signal("decisions")
    w.foreman("report", "app-decisions", "done", "all five committed")
    b = board(w)
    assert b["states"]["app-decisions"] == "done"
    assert "app-feature" in b["launchable"]


def test_6b_reports_wake_an_attached_pm_through_session_coord(w):
    w.foreman("attach", "--repo", str(w.repos / "app"), "--target", "pm-roadmap")
    w.foreman("report", "app-decisions", "running", "milestone one")
    assert not (w.fake / "msgs.txt").exists()  # milestones do not ring
    w.foreman("report", "app-decisions", "review", "PR #3 open")
    (m,) = [json.loads(x) for x in (w.fake / "msgs.txt").read_text().splitlines()]
    assert m[:4] == ["msg", "--repo", str(w.repos / "app"), "pm-roadmap"]
    assert "app-decisions review" in m[4]


# 7 -------------------------------------------------------------------------------------------
def test_7_a_decision_item_produces_exactly_one_ask_and_never_a_launch(w):
    w.foreman("pulse")
    w.foreman("pulse")
    w.foreman("pulse")  # three fresh processes: three PM wakes, or a PM restarted twice
    asks = [c for c in w.calls("session-coord") if c[:2] == ["ask", "add"]]
    assert len(asks) == 1
    a = asks[0]
    assert a[a.index("--kind") + 1] == "Q"
    text = a[a.index("--text") + 1]
    assert "Trust posture" in text and "pinned roots only" in text
    b = board(w)
    assert b["states"]["trust-posture"] == "ready"
    assert any(x.startswith("trust-posture (decision, asked Q-") for x in b["waiting"])

    r = w.foreman("launch", "trust-posture", check=False)
    assert r.returncode == 2 and "decision item" in r.stderr
    assert "trust-posture" not in b["launchable"]
    w.foreman("launch", "--ready", "--go", env={"TMUX": ""})
    assert all("trust-posture" not in json.dumps(c) for c in w.calls("tmux"))


def test_7b_losing_the_state_record_does_not_file_twice(w):
    """If state.json is lost after an ask was filed, the stable tag makes the refile a no-op."""
    w.foreman("pulse")
    (w.plan_dir / "state.json").unlink()
    out = w.foreman("pulse").stdout
    assert "escalated" in out
    assert len(w.asks()) == 1
    assert "was already filed" in (w.plan_dir / "events.jsonl").read_text()


# Beyond the seven -----------------------------------------------------------------------------
def test_dead_worker_is_detected_from_session_coord_and_can_be_relaunched(w):
    cfg = Path(os.environ["FOREMAN_CONFIG"])
    cfg.write_text("launch_grace_minutes = 0\n")
    w.foreman("launch", "app-decisions", "--go", env={"TMUX": ""})
    sid = json.loads((w.plan_dir / "state.json").read_text())["items"]["app-decisions"]["session"]

    w.set_board([sid])
    w.foreman("pulse")
    assert "app-decisions running live" in w.foreman("board").stdout

    w.set_board([])
    out = w.foreman("pulse").stdout
    assert "app-decisions worker gone" in out
    assert board(w)["workers"] == [0, 3]  # a dead worker holds no slot

    (w.fake / "board.json").write_text("not json")  # session-coord could not be read
    w.foreman("pulse")
    assert "running unknown" in w.foreman("board").stdout

    w.set_board([])
    w.foreman("pulse")
    out = w.foreman("launch", "--relaunch", "app-decisions", "--go", env={"TMUX": ""}).stdout
    call = w.calls("tmux")[-1]
    assert call[call.index("--resume") + 1] == sid
    assert "git" not in out.split("# launched")[0].split("\n", 1)[1]  # worktree reused


def test_a_relaunch_of_a_live_worker_is_refused(w):
    w.foreman("launch", "app-decisions", "--go", env={"TMUX": ""})
    r = w.foreman("launch", "--relaunch", "app-decisions", check=False)
    assert r.returncode == 2 and "second worker" in r.stderr


def test_merge_contention_is_on_the_board(w):
    w.foreman("launch", "app-decisions", "--go", env={"TMUX": ""})
    w.signal("decisions")
    w.foreman("pulse")
    (w.fake / "merged.txt").write_text("upstream/lib#1576\nupstream/lib#1577\n")
    w.foreman("pulse")
    out = w.foreman("board").stdout
    assert f"merges queue in {w.repos / 'app'}: app-feature, app-upstream" in out


def test_deadlines_flag_a_chain_that_cannot_finish(w):
    p = w.plan_dir / "plan.toml"
    t = p.read_text().replace('name = "roadmap"', 'name = "roadmap"\ndue = 2000-01-01')
    p.write_text(t)
    out = w.foreman("board").stdout
    assert "ext-lib: overdue (due 2000-01-01" in out


def test_a_worker_started_by_hand_can_be_adopted(w):
    """The first real plan began with one worker the human had already started himself."""
    core = w.repos / "core"
    w.set_board(["hand-started-1"])
    out = w.foreman("adopt", "app-decisions", "--session", "hand-started-1", "--pane", "%7",
                    "--worktree", str(w.repos / "app")).stdout
    assert "adopted" in out
    w.foreman("pulse")
    b = board(w)
    assert b["states"]["app-decisions"] == "running"
    assert b["workers"] == [1, 3]
    assert "app-decisions running live" in w.foreman("board").stdout
    assert w.calls("tmux") == []  # adoption launches nothing

    # Its reports count like any worker's.
    w.foreman("report", "app-decisions", "review", "PR #3 open")
    assert board(w)["states"]["app-decisions"] == "review"

    # Refused: an item that is not a build item, or one that already has a live worker.
    r = w.foreman("adopt", "trust-posture", "--session", "x", check=False)
    assert r.returncode == 2 and "decision item" in r.stderr
    r = w.foreman("adopt", "app-decisions", "--session", "other", check=False)
    assert r.returncode == 2 and "already has a worker" in r.stderr


def test_relaunching_an_adopted_worker_reuses_its_recorded_tree(w):
    cfg = Path(os.environ["FOREMAN_CONFIG"])
    cfg.write_text("launch_grace_minutes = 0\n")
    app = w.repos / "app"
    w.foreman("adopt", "app-decisions", "--session", "hand-1", "--worktree", str(app))
    w.set_board([])
    w.foreman("pulse")
    out = w.foreman("launch", "--relaunch", "app-decisions").stdout
    assert "worktree add" not in out
    line = next(l for l in out.splitlines() if l.startswith("tmux new-window"))
    assert f"-c {app} claude --resume hand-1" in line
