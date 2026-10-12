"""Processes a worker left behind: orphans, hangs, and runs in worktrees that no longer exist.

A worker can be live by every test foreman has and still be leaving damage behind. On the first
real plan (2026-10-12), a worker handed its test loop to Codex; Codex exited and left three full
pytest-xdist suites hung for 45 minutes at 0% CPU in the worker's worktree, still holding ports,
and the worker read as live throughout. A sweep then found two test processes idle for ten days
in a worktree deleted at merge. Nobody had looked, because nothing pointed anyone there.

What is checked, all without reading a transcript:

- a process whose working directory is inside a worker's worktree and whose parent is the
  init process or a subreaper (`systemd --user`): an orphan, its starter gone;
- one whose working directory has been deleted, which under `.worktrees/` means a worktree
  removed while something still ran in it;
- and, as a note on either, whether its CPU time has moved since the previous pulse.

Idleness alone is not a finding. A worker's own claude, its shell and its helpers sit in the
worktree at 0% CPU whenever it waits, and flagging them would bury the real cases.

foreman reports these. It kills only with `reap --go`, and only a process that is orphaned,
outside every tmux pane, not a claude, and in a deleted directory or under an item that is done
or abandoned. Everything else is the worker's to clean up, because killing a test run a live
worker is waiting on is worse than leaving it.

A deleted directory is not by itself a reason to kill. The first version treated it as one,
and foreman1 caught it before it fired: a live interactive Claude session whose cwd was a
removed worktree would have been killed (2026-10-12). Being in a pane, or being claude, now
forbids reaping outright.

Linux only (/proc). Elsewhere the scan answers None -- could not look -- and nothing is flagged.
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

DELETED = " (deleted)"


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    cwd: str            # with " (deleted)" appended by the kernel when the directory is gone
    cpu: int            # utime + stime, in clock ticks
    start: int          # start time in ticks since boot: with pid, the process's identity
    cmd: str
    parent_cmd: str = ""

    @property
    def deleted(self) -> bool:
        return self.cwd.endswith(DELETED)

    @property
    def path(self) -> str:
        return self.cwd[: -len(DELETED)] if self.deleted else self.cwd

    @property
    def orphan(self) -> bool:
        return self.ppid == 1 or self.parent_cmd in ("systemd", "init")

    @property
    def key(self) -> str:
        return f"{self.pid}:{self.start}"


def under(path: str, root: str) -> bool:
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


@dataclass(frozen=True)
class Finding:
    proc: Proc
    item: str           # owning item, or "" when it belongs to no item in the plan
    why: tuple[str, ...]
    reapable: bool      # true only when no live worker can be waiting on it


def protected_pids(procs: Iterable[Proc], roots: Iterable[int]) -> set[int]:
    """Every process at or below the given roots -- tmux pane processes -- plus every claude.
    Nothing in this set is ever reaped."""
    ps = list(procs)
    kids: dict[int, list[int]] = {}
    for p in ps:
        kids.setdefault(p.ppid, []).append(p.pid)
    out: set[int] = set()
    todo = list(roots)
    while todo:
        pid = todo.pop()
        if pid in out:
            continue
        out.add(pid)
        todo.extend(kids.get(pid, []))
    out.update(p.pid for p in ps if is_claude(p))
    return out


def is_claude(p: Proc) -> bool:
    first = p.cmd.split(" ", 1)[0] if p.cmd else ""
    return first.rsplit("/", 1)[-1] == "claude"


def analyse(procs: Iterable[Proc], trees: Mapping[str, tuple[str, str]],
            previous: Mapping[str, dict], now: float, idle_minutes: float,
            own_pids: Iterable[int] = (), protected: Iterable[int] | None = None) -> list[Finding]:
    """`protected` is `protected_pids(...)`; None means the panes could not be listed, and then
    nothing is reapable at all."""
    """`trees` maps a worktree path to (item, item state). `previous` is the last pulse's
    {key: {"cpu": int, "since": epoch}} record of CPU time, so idleness spans pulses."""
    skip = set(own_pids)
    guard = set(protected) if protected is not None else None
    out = []
    for p in procs:
        if p.pid in skip:
            continue
        owner = next(((item, st) for root, (item, st) in trees.items() if under(p.path, root)),
                     None)
        in_worktrees = "/.worktrees/" in p.path + "/"
        if owner is None and not (p.deleted and in_worktrees):
            continue
        why = []
        if p.deleted:
            why.append("its directory was deleted")
        if p.orphan and owner is not None:
            why.append("orphaned")
        if not why:
            continue
        prev = previous.get(p.key)
        if prev and prev.get("cpu") == p.cpu and now - prev.get("since", now) >= idle_minutes * 60:
            why.append(f"idle {int((now - prev['since']) // 60)}m")
        item, st = owner if owner else ("", "")
        reapable = (guard is not None and p.pid not in guard and p.orphan and not is_claude(p)
                    and (p.deleted or st in ("done", "abandoned")))
        out.append(Finding(p, item, tuple(why), reapable))
    return out


def next_cpu_record(procs: Iterable[Proc], previous: Mapping[str, dict], now: float,
                    trees: Mapping[str, tuple[str, str]]) -> dict[str, dict]:
    """What to remember for the next pulse: when each watched process's CPU last moved."""
    rec = {}
    for p in procs:
        if not (any(under(p.path, r) for r in trees) or (p.deleted and "/.worktrees/" in p.path)):
            continue
        prev = previous.get(p.key)
        since = prev["since"] if prev and prev.get("cpu") == p.cpu else now
        rec[p.key] = {"cpu": p.cpu, "since": since}
    return rec


# --- the adapter --------------------------------------------------------------------------

def scan(proc_root: str = "/proc") -> list[Proc] | None:
    """Every readable process. None when /proc is absent: could not look."""
    root = Path(proc_root)
    if not root.is_dir():
        return None
    raw = {}
    for d in root.iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
            cwd = os.readlink(d / "cwd")
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue  # gone, or another user's: not ours to judge
        # comm can contain spaces and parentheses; the fields after the LAST ')' are fixed.
        head, _, rest = stat.rpartition(")")
        f = rest.split()
        try:
            raw[int(d.name)] = (int(f[1]), cwd, int(f[11]) + int(f[12]), int(f[19]), cmd,
                                head.partition("(")[2])
        except (IndexError, ValueError):
            continue
    return [Proc(pid, ppid, cwd, cpu, start, cmd, raw.get(ppid, (0, "", 0, 0, "", ""))[5])
            for pid, (ppid, cwd, cpu, start, cmd, _comm) in raw.items()]


def kill(p: Proc, grace: float = 5.0) -> str:
    """TERM, then KILL after `grace` seconds -- only if the pid is still the same process."""
    def same() -> bool:
        cur = {q.pid: q for q in (scan() or [])}.get(p.pid)
        return cur is not None and cur.start == p.start
    if not same():
        return f"{p.pid} already gone"
    try:
        os.kill(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        return f"{p.pid} already gone"
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not same():
            return f"{p.pid} terminated"
        time.sleep(0.1)
    try:
        os.kill(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        return f"{p.pid} terminated"
    return f"{p.pid} killed"
