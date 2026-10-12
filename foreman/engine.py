"""The PM's verbs: pulse, launch, report, mark, attach.

Every verb reads the plan and the state from disk, does its work, and writes what it learned
back before returning. Nothing is carried between calls in memory, which is the whole of the
crash-and-restart story: there is nothing to lose.

The world -- session-coord, git, tmux, gh, the clock -- comes in through `World`, so each verb
can be tested against fakes and the acceptance scenarios run without a tmux server or a network.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import graph, launch as launch_mod, oracles, procs as procs_mod, store
from .config import Config
from .coord import Coord
from .plan import Item, Plan

REPORT_STATES = ("running", "review", "done", "stuck", "waiting")
#: Flags a report sets and a later `running`/`review`/`done` clears. `stuck`: the worker cannot go
#: on. `waiting`: it is paused on the user -- a decision gate inside the item, an approval.
FLAGS = ("stuck", "waiting")
MARK_STATES = ("done", "abandoned", "reset")
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


@dataclass
class World:
    coord: Coord = field(default_factory=Coord)
    oracle_run: oracles.Runner = oracles.default_runner
    run: launch_mod.Run = launch_mod.default_run
    now: Callable[[], dt.datetime] = utcnow
    tmux_session: Callable[[], str] = lambda: ""
    cwd: Callable[[], str] = lambda: str(Path.cwd())
    pane: Callable[[], str] = lambda: os.environ.get("TMUX_PANE", "")
    #: (pane, session id) -> True if a process in that pane carries the session id in its argv,
    #: False if not, None if tmux or the process table could not be read.
    pane_has_session: Callable[[str, str], "bool | None"] = lambda pane, sid: None
    #: Every process, or None when the process table cannot be read.
    scan: Callable[[], "list[procs_mod.Proc] | None"] = lambda: None
    #: A pane's last activity as epoch seconds, or None when tmux cannot say.
    pane_activity: Callable[[str], "float | None"] = lambda pane: None


class EngineError(RuntimeError):
    """A verb refused. The message says why and what to do instead."""


def _iso(t: dt.datetime) -> str:
    return t.isoformat()


def _parse(s: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def ask_tag(plan: Plan, item_id: str) -> str:
    """A stable tag per (plan, item), so filing twice is visibly the same ask."""
    h = int.from_bytes(hashlib.sha256(f"{plan.name}\0{item_id}".encode()).digest()[:4], "big")
    return "Q-" + "".join(CROCKFORD[(h >> (5 * i)) & 31] for i in range(4))


def _event(plan_dir: Path, now: dt.datetime, item: str, what: str, **kw: Any) -> None:
    store.append_event(plan_dir, {"t": _iso(now), "item": item, "what": what, **kw})


def _clear_flags(rec: dict[str, Any]) -> None:
    for f in FLAGS:
        rec.pop(f, None)


def _rec(doc: dict[str, Any], iid: str) -> dict[str, Any]:
    return doc["items"].setdefault(iid, {})


# --- pulse --------------------------------------------------------------------------------

def _needs_oracle(it: Item, st: str) -> bool:
    return st not in graph.FINAL and bool(it.oracle) and not oracles.is_manual(it.oracle)


def pulse(plan: Plan, plan_dir: Path, cfg: Config, world: World) -> dict[str, Any]:
    """Check oracles and workers, file escalations, and return what changed.

    Oracles and session-coord are consulted outside the lock, because a slow `gh` would
    otherwise hold every worker's `report` waiting. Their results are then applied in one
    transaction against a fresh read, so a report that landed meanwhile is not overwritten.
    """
    now = world.now()
    snap = store.read(plan_dir)
    sts = graph.states(plan, snap)

    verdicts: dict[str, oracles.Verdict] = {}
    for iid, it in plan.items.items():
        if _needs_oracle(it, sts[iid]):
            cwd = it.repo if it.repo and Path(it.repo).is_dir() else str(plan_dir)
            verdicts[iid] = oracles.check(it.oracle, cwd, world.oracle_run)

    live_by_repo: dict[str, set[str] | None] = {}
    for iid, it in plan.items.items():
        if sts[iid] in graph.ACTIVE and snap["items"].get(iid, {}).get("session") and it.repo:
            if it.repo not in live_by_repo:
                live_by_repo[it.repo] = world.coord.live_sessions(it.repo)

    changes: list[str] = []
    with store.transaction(plan_dir) as doc:
        for iid, v in verdicts.items():
            rec = _rec(doc, iid)
            prev = rec.get("oracle", {}).get("ok")
            rec["oracle"] = {"ok": v.ok, "detail": v.detail, "checked": _iso(now)}
            if v.ok is True and rec.get("state") not in graph.FINAL:
                rec["state"] = "done"
                _clear_flags(rec)
                changes.append(f"{iid} done: {v.detail}")
                _event(plan_dir, now, iid, "oracle", ok=True, detail=v.detail)
            elif v.ok != prev:
                _event(plan_dir, now, iid, "oracle", ok=v.ok, detail=v.detail)

        sts = graph.states(plan, doc)
        grace = dt.timedelta(minutes=cfg.launch_grace_minutes)
        for iid, it in plan.items.items():
            rec = doc["items"].get(iid, {})
            if sts[iid] not in graph.ACTIVE or not rec.get("session"):
                continue
            live = live_by_repo.get(it.repo)
            launched = _parse(rec.get("launched", ""))
            in_pane = (world.pane_has_session(rec["pane"], rec["session"])
                       if rec.get("pane") and (live is None or rec["session"] not in live)
                       else None)
            if live is not None and rec["session"] in live:
                worker = "live"
            elif in_pane:
                worker = "live"
            elif live is None:
                worker = "unknown"
            elif launched and now - launched < grace:
                worker = "starting"
            else:
                worker = "dead"
            if worker != rec.get("worker"):
                if worker == "dead":
                    changes.append(f"{iid} worker gone")
                _event(plan_dir, now, iid, "worker", worker=worker)
            rec["worker"] = worker

        if cfg.escalate == "session-coord":
            changes += _escalate(plan, plan_dir, doc, sts, world, now)
        _quiet_panes(plan, doc, sts, world)
        changes += _leftovers(plan, doc, graph.states(plan, doc), cfg, world, now)
    return {"changes": changes}


def trees(plan: Plan, doc: dict[str, Any], sts: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Each recorded worker tree, mapped to (item, state). A tree that is a repo's main
    checkout is left out: everything run there is somebody's, not this worker's leftovers."""
    out = {}
    for iid, it in plan.items.items():
        wt = doc["items"].get(iid, {}).get("worktree", "")
        if wt and wt.rstrip("/") != it.repo.rstrip("/"):
            out[wt.rstrip("/")] = (iid, sts[iid])
    return out


def _leftovers(plan: Plan, doc: dict[str, Any], sts: dict[str, str], cfg: Config,
               world: World, now: dt.datetime) -> list[str]:
    ps = world.scan()
    if ps is None:
        return []  # could not look; keep the last findings rather than clearing them
    t = trees(plan, doc, sts)
    epoch = now.timestamp()
    found = procs_mod.analyse(ps, t, doc.get("procs", {}), epoch, cfg.idle_minutes,
                              own_pids=(os.getpid(),))
    doc["procs"] = procs_mod.next_cpu_record(ps, doc.get("procs", {}), epoch, t)
    before = {f["key"] for f in doc.get("leftovers", [])}
    doc["leftovers"] = [{"key": f.proc.key, "pid": f.proc.pid, "item": f.item,
                         "why": list(f.why), "cwd": f.proc.cwd, "cmd": f.proc.cmd[:120],
                         "reapable": f.reapable} for f in found]
    new = [f for f in found if f.proc.key not in before]
    return [f"leftover pid {f.proc.pid} ({f.item or 'no item'}): {', '.join(f.why)}"
            for f in new]


def _quiet_panes(plan: Plan, doc: dict[str, Any], sts: dict[str, str], world: World) -> None:
    for iid in plan.items:
        rec = doc["items"].get(iid, {})
        if sts[iid] == "running" and rec.get("pane") and rec.get("worker") != "dead":
            t = world.pane_activity(rec["pane"])
            if t is not None:
                rec["pane_activity"] = t


def _escalate(plan: Plan, plan_dir: Path, doc: dict[str, Any], sts: dict[str, str],
              world: World, now: dt.datetime) -> list[str]:
    """One ask per ready decision, recorded so that no restart files it twice."""
    out = []
    repo = doc.get("pm", {}).get("repo") or world.cwd()
    for iid, it in plan.items.items():
        if it.kind != "decision" or sts[iid] != "ready":
            continue
        rec = _rec(doc, iid)
        if rec.get("ask"):
            continue
        tag = ask_tag(plan, iid)
        unblocks = [plan.items[d].title for d in plan.dependents(iid)]
        text = (f"Decision for plan '{plan.name}' -- {it.title}: "
                f"{it.question or 'see the plan item for the options'}"
                + (f" Answering unblocks: {'; '.join(unblocks)}." if unblocks else "")
                + " Tell the project-manager session your answer and it will close the item.")
        deps = [doc["items"].get(d, {}).get("ask") for d in it.depends_on]
        ok, note = world.coord.ask_add(repo, tag, "Q", text, [d for d in deps if d])
        if ok:
            rec["ask"] = tag
            rec["ask_repo"] = repo
            out.append(f"{iid} escalated as {tag}")
            _event(plan_dir, now, iid, "escalate", tag=tag, repo=repo, note=note)
        else:
            rec["ask_error"] = note
            out.append(f"{iid} escalation failed: {note}")
    return out


# --- launch -------------------------------------------------------------------------------

def slots(plan: Plan, doc: dict[str, Any], cfg: Config) -> tuple[int, int]:
    """(live workers, cap). A worker known to be dead does not hold a slot; unknown does."""
    sts = graph.states(plan, doc)
    used = sum(1 for iid in plan.items
               if sts[iid] in graph.ACTIVE and doc["items"].get(iid, {}).get("session")
               and doc["items"][iid].get("worker") != "dead")
    return used, cfg.cap(plan.max_workers)


def prepare(plan: Plan, plan_dir: Path, cfg: Config, world: World, ids: list[str],
            relaunch: bool = False) -> list[launch_mod.Launch]:
    """The launches that would happen for `ids`, refusing anything the rules forbid."""
    doc = store.read(plan_dir)
    sts = graph.states(plan, doc)
    used, cap = slots(plan, doc, cfg)
    out = []
    for iid in ids:
        it = plan.items.get(iid)
        if it is None:
            raise EngineError(f"no item {iid!r} in plan {plan.name!r}.")
        if it.kind != "build":
            raise EngineError(f"{iid} is a {it.kind} item; only build items get a worker. "
                              + ("Decisions are the user's." if it.kind == "decision" else
                                 "External items wait on the world; foreman only polls them."))
        rec = doc["items"].get(iid, {})
        session = None
        if relaunch:
            if sts[iid] not in graph.ACTIVE or not rec.get("session"):
                raise EngineError(f"{iid} has no worker to relaunch (state {sts[iid]}).")
            if rec.get("worker") not in ("dead",):
                raise EngineError(f"{iid}'s worker is {rec.get('worker', 'unchecked')}, not "
                                  "dead; relaunching would start a second worker. Run pulse.")
            session = rec["session"]
        else:
            if sts[iid] != "ready":
                raise EngineError(f"{iid} is {sts[iid]}, not ready.")
            if used >= cap:
                raise EngineError(f"{used} of {cap} workers are live; {iid} waits for a slot.")
            used += 1
        kept = rec.get("worktree", "") if relaunch else ""
        if kept and not launch_mod.worktree_exists(kept):
            kept = ""
        wt = kept or f"{it.repo.rstrip('/')}/.worktrees/{it.topic}"
        exists = launch_mod.worktree_exists(wt)
        default = it.base or ("" if exists else launch_mod.default_branch(it.repo, world.run))
        out.append(launch_mod.plan_launch(
            plan, it, tmux_session=cfg.tmux_session or world.tmux_session(),
            claude=cfg.claude, default_branch=default, worktree_exists=exists,
            branch_exists=False if exists else launch_mod.branch_exists(it.repo, it.topic, world.run),
            session=session, worktree=kept))
    return out


def ready_to_fill(plan: Plan, plan_dir: Path, cfg: Config) -> list[str]:
    doc = store.read(plan_dir)
    used, cap = slots(plan, doc, cfg)
    return graph.launchable(plan, graph.states(plan, doc))[:max(cap - used, 0)]


def commit_launch(plan: Plan, plan_dir: Path, cfg: Config, world: World,
                  ln: launch_mod.Launch) -> dict[str, str]:
    """Execute one launch and record it. The cap is re-checked under the lock."""
    now = world.now()
    with store.locked(plan_dir):
        doc = store.read(plan_dir)
        used, cap = slots(plan, doc, cfg)
        if not ln.resume and used >= cap:
            raise EngineError(f"{used} of {cap} workers are live; {ln.item} waits for a slot.")
        got = launch_mod.execute(ln, world.run)
        rec = _rec(doc, ln.item)
        rec.update({"state": rec.get("state") if ln.resume else "running",
                    "session": ln.session, "pane": got.get("pane", ""),
                    "worktree": ln.worktree, "launched": _iso(now), "last_report": _iso(now),
                    "worker": "starting"})
        rec["last_line"] = "relaunched" if ln.resume else "launched"
        store.write(plan_dir, doc)
        _event(plan_dir, now, ln.item, "relaunch" if ln.resume else "launch",
               session=ln.session, pane=got.get("pane", ""), worktree=ln.worktree)
    return got


def adopt(plan: Plan, plan_dir: Path, world: World, iid: str, session: str, pane: str,
          worktree: str, note: str) -> str:
    """Take on a worker someone started by hand, so foreman watches it like its own.

    Nothing is launched. The cap is not enforced, because the worker already exists; refusing
    would only make the record disagree with the world.
    """
    it = plan.items.get(iid)
    if it is None:
        raise EngineError(f"no item {iid!r} in plan {plan.name!r}.")
    if it.kind != "build":
        raise EngineError(f"{iid} is a {it.kind} item; only build items have workers.")
    now = world.now()
    with store.transaction(plan_dir) as doc:
        sts = graph.states(plan, doc)
        rec = _rec(doc, iid)
        if sts[iid] in graph.FINAL:
            raise EngineError(f"{iid} is already {sts[iid]}.")
        if rec.get("session") and rec["session"] != session and rec.get("worker") != "dead":
            raise EngineError(f"{iid} already has a worker ({rec['session']}, "
                              f"{rec.get('worker', 'unchecked')}); `foreman mark {iid} reset` "
                              "first if that one is gone.")
        rec.update({"state": rec.get("state") if sts[iid] in graph.ACTIVE else "running",
                    "session": session, "pane": pane,
                    "worktree": worktree or f"{it.repo.rstrip('/')}/.worktrees/{it.topic}",
                    "launched": _iso(now), "last_report": _iso(now), "worker": "starting",
                    "last_line": note or "adopted"})
        _event(plan_dir, now, iid, "adopt", session=session, pane=pane,
               worktree=rec["worktree"], note=note)
    msg = f"{iid} adopted: session {session}" + (f", pane {pane}" if pane else "")
    if not Path(rec["worktree"]).is_dir():
        msg += (f"\nwarning: {rec['worktree']} does not exist yet. A relaunch would create it "
                "from origin; re-adopt with --worktree once the worker has moved.")
    return msg


# --- report, mark, attach -----------------------------------------------------------------

def report(plan: Plan, plan_dir: Path, world: World, iid: str, state: str, line: str) -> str:
    """A worker's word. `done` is checked against the oracle and is not believed alone."""
    if iid not in plan.items:
        raise EngineError(f"no item {iid!r} in plan {plan.name!r}.")
    if state not in REPORT_STATES:
        raise EngineError(f"report state must be one of {list(REPORT_STATES)}.")
    it = plan.items[iid]
    now = world.now()
    verdict = None
    if state == "done" and it.oracle and not oracles.is_manual(it.oracle):
        cwd = it.repo if it.repo and Path(it.repo).is_dir() else str(plan_dir)
        verdict = oracles.check(it.oracle, cwd, world.oracle_run)
    with store.transaction(plan_dir) as doc:
        rec = _rec(doc, iid)
        if rec.get("state") in graph.FINAL or it.pinned:
            outcome = f"{iid} is already {it.pinned or rec['state']}; report recorded only"
        elif state in FLAGS:
            rec[state] = line
            outcome = f"{iid} flagged {state}"
        elif state == "done":
            if verdict is not None and verdict.ok is True:
                rec["state"] = "done"
                _clear_flags(rec)
                outcome = f"{iid} done; oracle agrees ({verdict.detail})"
            else:
                rec["state"] = "review"
                rec["claimed_done"] = True
                why = verdict.detail if verdict else "its oracle is manual"
                outcome = f"{iid} claimed done, held in review: {why}"
            if verdict is not None:
                rec["oracle"] = {"ok": verdict.ok, "detail": verdict.detail, "checked": _iso(now)}
        else:
            rec["state"] = state
            _clear_flags(rec)
            outcome = f"{iid} {state}"
        rec["last_report"] = _iso(now)
        rec["last_line"] = line
        _event(plan_dir, now, iid, "report", state=state, line=line, outcome=outcome)
        pm = dict(doc.get("pm", {}))
    from_pm = bool(pm.get("pane")) and pm.get("pane") == world.pane()
    if state in ("review", "done") + FLAGS and pm.get("repo") and pm.get("target") and not from_pm:
        world.coord.msg(pm["repo"], pm["target"],
                        f"foreman: {outcome}. Worker says: {line}. Run `foreman pulse --plan "
                        f"{plan.name}`.")
    return outcome


def mark(plan: Plan, plan_dir: Path, world: World, iid: str, state: str, note: str) -> str:
    """A human or the PM overriding the record: close a manual item, abandon, or reset."""
    if iid not in plan.items:
        raise EngineError(f"no item {iid!r} in plan {plan.name!r}.")
    if state not in MARK_STATES:
        raise EngineError(f"mark state must be one of {list(MARK_STATES)}.")
    now = world.now()
    with store.transaction(plan_dir) as doc:
        rec = _rec(doc, iid)
        if state == "reset":
            doc["items"][iid] = {k: v for k, v in rec.items() if k in ("ask", "ask_repo")}
        else:
            rec["state"] = state
            _clear_flags(rec)
        rec = doc["items"][iid]
        rec["note"] = note
        _event(plan_dir, now, iid, "mark", state=state, note=note)
    return f"{iid} marked {state}"


def attach(plan_dir: Path, world: World, repo: str, target: str, pane: str = "") -> str:
    """Record where the PM session is, so worker reports can wake it through session-coord."""
    with store.transaction(plan_dir) as doc:
        doc["pm"] = {"repo": repo, "target": target, "pane": pane, "since": _iso(world.now())}
    return f"worker reports will message {target} in {repo}"


def stalled(plan: Plan, doc: dict[str, Any], cfg: Config, now: dt.datetime) -> list[str]:
    sts = graph.states(plan, doc)
    out = []
    for iid in plan.items:
        rec = doc["items"].get(iid, {})
        last = _parse(rec.get("last_report", ""))
        if sts[iid] == "running" and last and now - last > dt.timedelta(hours=cfg.stall_hours):
            out.append(iid)
    return out


def reap(plan: Plan, plan_dir: Path, cfg: Config, world: World, go: bool,
         kill=procs_mod.kill) -> list[str]:
    """Leftovers no live worker can be waiting on. Lists them; kills them only with `go`."""
    ps = world.scan()
    if ps is None:
        raise EngineError("the process table cannot be read here (no /proc); nothing to reap.")
    doc = store.read(plan_dir)
    sts = graph.states(plan, doc)
    found = procs_mod.analyse(ps, trees(plan, doc, sts), doc.get("procs", {}),
                              world.now().timestamp(), cfg.idle_minutes, own_pids=(os.getpid(),))
    out = []
    for f in found:
        line = (f"pid {f.proc.pid} [{f.item or 'no item'}] {', '.join(f.why)}: "
                f"{f.proc.cmd[:80]}  (cwd {f.proc.cwd})")
        if not f.reapable:
            out.append(f"leave  {line} -- a live worker may own it; tell the worker")
        elif go:
            out.append(f"{kill(f.proc)}  {line}")
        else:
            out.append(f"would kill  {line}")
    return out
