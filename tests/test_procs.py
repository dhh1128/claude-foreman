from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from foreman import procs
from foreman.procs import Proc

WT = "/r/.worktrees/a"
TREES = {WT: ("a", "running"), "/r/.worktrees/b": ("b", "done")}


def P(pid, cwd, ppid=500, cpu=10, parent="bash", cmd="pytest"):
    return Proc(pid, ppid, cwd, cpu, 1000 + pid, cmd, parent)


def test_orphans_in_a_worktree_are_found_and_idleness_is_noted():
    ps = [P(1, WT + "/tests", ppid=1, parent=""), P(2, WT, parent="claude"),
          P(3, WT, ppid=42, parent="systemd"), P(4, "/elsewhere", ppid=1)]
    prev = {"3:1003": {"cpu": 10, "since": 0.0}}
    f = {x.proc.pid: x for x in procs.analyse(ps, TREES, prev, now=3600.0, idle_minutes=30)}
    assert set(f) == {1, 3}
    assert f[1].why == ("orphaned",) and f[1].item == "a"
    assert f[3].why == ("orphaned", "idle 60m")
    assert not f[1].reapable  # a live worker may still be waiting on it


def test_a_quiet_process_that_is_not_orphaned_is_not_a_finding():
    """The worker's own claude and shell idle in its worktree whenever it waits."""
    prev = {"2:1002": {"cpu": 10, "since": 0.0}}
    assert procs.analyse([P(2, WT, parent="claude")], TREES, prev, 99999.0, 30) == []


def test_deleted_worktrees_are_reapable_even_when_no_plan_owns_them():
    ps = [P(5, "/x/.worktrees/gone (deleted)", ppid=1), P(6, "/tmp/gone (deleted)", ppid=1)]
    (f,) = procs.analyse(ps, TREES, {}, 0.0, 30, protected=set())
    assert f.proc.pid == 5 and f.item == "" and f.reapable
    (f,) = procs.analyse(ps, TREES, {}, 0.0, 30)  # panes unknown: nothing is reapable
    assert not f.reapable
    assert f.why == ("its directory was deleted",)


def test_orphans_of_a_finished_item_are_reapable():
    (f,) = procs.analyse([P(7, "/r/.worktrees/b", ppid=1)], TREES, {}, 0.0, 30, protected=set())
    assert f.item == "b" and f.reapable


def test_cpu_record_carries_since_forward_only_while_cpu_is_still():
    ps = [P(1, WT, cpu=10)]
    r1 = procs.next_cpu_record(ps, {}, 100.0, TREES)
    assert r1 == {"1:1001": {"cpu": 10, "since": 100.0}}
    assert procs.next_cpu_record(ps, r1, 200.0, TREES)["1:1001"]["since"] == 100.0
    moved = [P(1, WT, cpu=11)]
    assert procs.next_cpu_record(moved, r1, 200.0, TREES)["1:1001"]["since"] == 200.0
    assert procs.next_cpu_record([P(9, "/elsewhere")], {}, 1.0, TREES) == {}


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="Linux /proc")
def test_scan_sees_a_real_orphan_and_its_deleted_directory(tmp_path):
    wt = tmp_path / ".worktrees" / "t"
    wt.mkdir(parents=True)
    # The shell starts sleep in the background and exits, so sleep is reparented.
    subprocess.run(["sh", "-c", "sleep 60 >/dev/null 2>&1 &"], cwd=wt, check=True)
    time.sleep(0.3)
    mine = [p for p in procs.scan() if p.path == str(wt) and p.cmd.startswith("sleep")]
    assert len(mine) == 1 and mine[0].orphan
    os.rmdir(wt)
    (p,) = [p for p in procs.scan() if p.pid == mine[0].pid]
    assert p.deleted and p.path == str(wt)
    (f,) = [f for f in procs.analyse([p], {}, {}, 0.0, 30, protected=set())]
    assert f.reapable
    assert procs.kill(p, grace=2.0) in (f"{p.pid} terminated", f"{p.pid} killed")
    assert p.pid not in {q.pid for q in procs.scan()}
