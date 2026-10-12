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
    #: The pid of every tmux pane's process, or None when tmux cannot be asked. Anything under
    #: a pane is somebody's live session and is never reaped.
    pane_pids: Callable[[], "list[int] | None"] = lambda: None


class EngineError(RuntimeError):
    """A verb refused. The message says why and what to do instead."""


def _iso(t: dt.datetime) -> str:
    return t.isoformat()


def _parse(s: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def ask_tag(plan: Plan, item_id: str, kind: str = "Q") -> str:
    """A stable tag per (plan, item), so filing twice is visibly the same ask."""
    h = int.from_bytes(hashlib.sha256(f"{plan.name}\0{item_id}".encode()).digest()[:4], "big")
    return f"{kind}-" + "".join(CROCKFORD[(h >> (5 * i)) & 31] for i in range(4))


def user_name() -> str:
    from .config import env
    return env("USER") or os.environ.get("SESSION_COORD_USER") or "the user"


def waits_on_user(it: Item) -> bool:
    w = it.waiting_on.strip().lower()
    return bool(w) and w in ("user", user_name().lower())


def waited_days(doc: dict[str, Any], iid: str, now: dt.datetime) -> float | None:
    since = _parse(doc["items"].get(iid, {}).get("ready_since", ""))
    return (now - since).total_seconds() / 86400 if since else None


def ask_due(plan: Plan, doc: dict[str, Any], it: Item, sts: dict[str, str],
            now: dt.datetime) -> tuple[bool, str]:
    """Whether a decision's `ask_when` has arrived, and the condition, for the board."""
    if not it.ask_when:
        return True, ""
    from .plan import parse_ask_when
    cond = parse_ask_when(it.ask_when, plan.items)
    if cond[0] == "date":
        return now.date() >= cond[1], it.ask_when
    _, other, days = cond
    if sts[other] in graph.FINAL:
        return False, f"{other} finished; not needed"
    waited = waited_days(doc, other, now)
    return (waited is not None and waited > days), it.ask_when


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


def pulse(plan: Plan, plan_dir: Path, cfg: Config, world: World,
          oracles_due: bool = True) -> dict[str, Any]:
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
        if oracles_due and _needs_oracle(it, sts[iid]):
            cwd = it.repo if it.repo and Path(it.repo).is_dir() else str(plan_dir)
            verdicts[iid] = oracles.check(it.oracle, cwd, world.oracle_run)

    live_by_repo: dict[str, set[str] | None] = {}
    for iid, it in plan.items.items():
        if sts[iid] in graph.ACTIVE and snap["items"].get(iid, {}).get("session") and it.repo:
            if it.repo not in live_by_repo:
                live_by_repo[it.repo] = world.coord.live_sessions(it.repo)

    pin_values: dict[str, str | None] = {}
    for name, pin in (plan.pins.items() if oracles_due else ()):
        cwd = pin.repo if pin.repo and Path(pin.repo).is_dir() else str(plan_dir)
        try:
            rc, out = world.oracle_run(["sh", "-c", pin.cmd], cwd, oracles.CMD_TIMEOUT)
            pin_values[name] = out.strip() if rc == 0 and out.strip() else None
        except Exception:  # noqa: BLE001 -- any failure to run is "could not look"
            pin_values[name] = None

    changes: list[str] = []
    with store.transaction(plan_dir) as doc:
        for iid, v in verdicts.items():
            rec = _rec(doc, iid)
            prev = rec.get("oracle", {}).get("ok")
            rec["oracle"] = {"ok": v.ok, "detail": v.detail, "checked": _iso(now)}
            if v.ok is True and rec.get("state") not in graph.FINAL and not rec.get("reopened"):
                rec["state"] = "done"
                _clear_flags(rec)
                changes.append(f"{iid} done: {v.detail}" + _ripple(plan, doc, iid))
                _event(plan_dir, now, iid, "oracle", ok=True, detail=v.detail)
            elif v.ok != prev:
                _event(plan_dir, now, iid, "oracle", ok=v.ok, detail=v.detail)

        sts = graph.states(plan, doc)
        grace = dt.timedelta(minutes=cfg.launch_grace_minutes)
        for iid, it in plan.items.items():
            rec = doc["items"].get(iid, {})
            if sts[iid] not in graph.ACTIVE or not (rec.get("session") or rec.get("pane")
                                                    or rec.get("wid")):
                continue
            worker = _liveness(rec, live_by_repo.get(it.repo) if rec.get("session") else None,
                               world, now, grace)
            if worker != rec.get("worker"):
                if worker == "dead":
                    changes.append(f"{iid} worker gone")
                _event(plan_dir, now, iid, "worker", worker=worker)
            rec["worker"] = worker
            if rec.get("wid") in doc.get("workers", {}):
                doc["workers"][rec["wid"]]["worker"] = worker

        changes += _pins(plan, plan_dir, doc, pin_values, now)
        _ready_since(plan, doc, graph.states(plan, doc), now)
        if cfg.escalate == "session-coord":
            changes += _escalate(plan, plan_dir, doc, sts, world, now)
        _quiet_panes(plan, doc, sts, world)
        if cfg.admission and graph.launchable(plan, graph.states(plan, doc)):
            ok, why = admit(cfg, world)
            doc["admission"] = {"ok": ok, "why": why, "at": _iso(now)}
        else:
            doc.pop("admission", None)
        changes += _leftovers(plan, doc, graph.states(plan, doc), cfg, world, now)
    return {"changes": changes}


def _liveness(rec: dict[str, Any], live: set[str] | None, world: World, now: dt.datetime,
              grace: dt.timedelta) -> str:
    """live, starting, dead or unknown, from whatever proof this worker's kind allows.

    - session-coord's board, for any worker with a Claude session id;
    - the recorded pane, for a worker zamu launched: its argv carries a token
      (`zamu-worker:<id>`) that disappears when the engine exits, so its absence is evidence;
    - for a worker someone else started with no session id (a Codex window), only whether its
      pane still exists;
    - a subagent has neither, and is `unknown` unless the PM reports for it.
    """
    sid, pane = rec.get("session", ""), rec.get("pane", "")
    if not sid and not pane:
        return "unknown"
    if not pane:
        in_pane = None
    elif rec.get("ours"):
        # zamu put the token in the argv, so its absence is evidence too.
        in_pane = world.pane_has_session(pane, rec.get("token") or sid)
    elif sid:
        # Someone else started it. Finding the session id in the pane's process tree proves it
        # live; not finding it proves nothing, because a hand-started claude's argv need not
        # carry its id. (Regression of 2026-10-12: three adopted workers whose pane process IS
        # claude were marked gone when this case was left out.)
        in_pane = True if world.pane_has_session(pane, sid) else None
    else:
        in_pane = world.pane_has_session(pane, "")  # a window with no id: does the pane exist?
    if (sid and live is not None and sid in live) or in_pane:
        return "live"
    if in_pane is None and (not sid or live is None):
        return "unknown"
    launched = _parse(rec.get("launched", ""))
    if launched and now - launched < grace:
        return "starting"
    return "dead"


def _ripple(plan: Plan, doc: dict[str, Any], iid: str) -> str:
    """When an item lands, the other items in flight in its repo have a moved base."""
    mine = set(plan.items[iid].repos)
    if not mine:
        return ""
    sts = graph.states(plan, doc)
    hit = {}
    for o, it in plan.items.items():
        shared = mine & set(it.repos)
        if o != iid and shared and sts[o] in graph.ACTIVE:
            hit[o] = sorted(shared)
    if not hit:
        return ""
    where = sorted({r for rs in hit.values() for r in rs})
    return f"; base moved for {', '.join(hit)} in {', '.join(where)}"


def _pins(plan: Plan, plan_dir: Path, doc: dict[str, Any], values: dict[str, str | None],
          now: dt.datetime) -> list[str]:
    out = []
    pins = doc.setdefault("pins", {})
    for name, value in values.items():
        if value is None:
            continue  # could not look: keep the last value, never treat it as a move
        prev = pins.get(name, {}).get("value")
        if prev == value:
            pins[name]["checked"] = _iso(now)
            continue
        pins[name] = {"value": value, "since": _iso(now), "checked": _iso(now),
                      **({"previous": prev} if prev else {})}
        if prev is None:
            continue  # first sight is a baseline, not a move
        pin = plan.pins[name]
        frozen = pin.freeze is not None and now.date() > pin.freeze
        stale = []
        for iid, it in plan.items.items():
            rec = doc["items"].get(iid, {})
            if name in it.rests_on and graph.states(plan, doc)[iid] == "done":
                rec["stale"] = f"pin {name} moved {prev} -> {value} on {now.date()}"
                stale.append(iid)
        _event(plan_dir, now, "", "pin", pin=name, previous=prev, value=value, stale=stale,
               after_freeze=frozen)
        out.append(f"pin {name} moved {prev} -> {value}"
                   + (f" AFTER ITS FREEZE ({pin.freeze})" if frozen else "")
                   + (f"; evidence now stale for {', '.join(stale)} (`zamu verify <id>`)"
                      if stale else ""))
    return out


def _pin_snapshot(doc: dict[str, Any]) -> dict[str, str]:
    return {k: v.get("value", "") for k, v in doc.get("pins", {}).items()}


def _add_evidence(doc: dict[str, Any], rec: dict[str, Any], ref: str, by: str,
                  now: dt.datetime) -> None:
    rec.setdefault("evidence", []).append({"at": _iso(now), "ref": ref, "by": by,
                                           "pins": _pin_snapshot(doc)})


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
    roots = world.pane_pids()
    found = procs_mod.analyse(ps, t, doc.get("procs", {}), epoch, cfg.idle_minutes,
                              own_pids=(os.getpid(),),
                              protected=None if roots is None else procs_mod.protected_pids(ps, roots))
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


def _ready_since(plan: Plan, doc: dict[str, Any], sts: dict[str, str], now: dt.datetime) -> None:
    """When each item last became ready or active, so waits can be aged and nagged about."""
    for iid in plan.items:
        rec = doc["items"].get(iid)
        if sts[iid] in ("ready",) + graph.ACTIVE:
            if rec is None:
                rec = doc["items"][iid] = {}
            rec.setdefault("ready_since", _iso(now))
        elif rec is not None:
            rec.pop("ready_since", None)


def _escalate(plan: Plan, plan_dir: Path, doc: dict[str, Any], sts: dict[str, str],
              world: World, now: dt.datetime) -> list[str]:
    """One ask per ready decision (Q), and per ready external item waiting on the user (A),
    recorded so that no restart files it twice. A decision with `ask_when` is held until then."""
    out = []
    repo = doc.get("pm", {}).get("repo") or world.cwd()
    if not repo:
        return []  # nowhere to file: no PM attached, and the caller has no project of its own
    for iid, it in plan.items.items():
        if sts[iid] != "ready":
            continue
        if it.kind == "decision":
            due, _cond = ask_due(plan, doc, it, sts, now)
            if not due:
                continue
            kind = "Q"
        elif it.kind == "external" and waits_on_user(it):
            kind = "A"
        else:
            continue
        rec = _rec(doc, iid)
        if rec.get("ask"):
            continue
        tag = ask_tag(plan, iid, kind)
        unblocks = [plan.items[d].title for d in plan.dependents(iid)]
        if kind == "Q":
            text = (f"Decision for plan '{plan.name}' -- {it.title}: "
                    f"{it.question or 'see the plan item for the options'}"
                    + (f" Answering unblocks: {'; '.join(unblocks)}." if unblocks else "")
                    + " Tell the project-manager session your answer and it will close the item.")
        else:
            text = (f"For plan '{plan.name}', please do this yourself: {it.title}."
                    + (f" {it.question}" if it.question else "")
                    + (f" Details: {it.brief}." if it.brief else "")
                    + (f" It unblocks: {'; '.join(unblocks)}." if unblocks else "")
                    + " zamu notices by itself when it is done; nothing to report back.")
        deps = [doc["items"].get(d, {}).get("ask") for d in it.depends_on]
        ok, note = world.coord.ask_add(repo, tag, kind, text, [d for d in deps if d])
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

def _busy(items: dict[str, Any], states: dict[str, str] | None = None) -> set[str]:
    """Worker ids holding a slot. A worker known to be dead holds none; unknown does. One
    worker carrying two items holds one slot."""
    busy = set()
    for iid, rec in items.items():
        st = states.get(iid) if states is not None else rec.get("state")
        if (st in graph.ACTIVE and (rec.get("session") or rec.get("pane") or rec.get("wid"))
                and rec.get("worker") != "dead"):
            busy.add(rec.get("wid") or iid)
    return busy


def busy_elsewhere(plan: Plan) -> dict[str, int]:
    """Live workers in every OTHER plan under the state root, by plan name.

    The cap is the machine's, so it has to be counted across plans. The first time two plans
    ran at once, the second PM's board said 0 of 4 while the first had 4 live, and `launch
    --ready --go` would have started four more (foreman-imbu, 2026-10-12). Other plans are read
    from their state alone, so a plan file mid-edit elsewhere cannot break this one. A state
    that cannot be read is counted as zero and named, never guessed at.
    """
    from . import config
    here = plan.path.parent.resolve()
    out: dict[str, int] = {}
    root = config.plans_dir()
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        try:
            if d.resolve() == here or not (d / "plan.toml").exists():
                continue
            n = len(_busy(store.read(d).get("items", {})))
        except (OSError, store.StoreError):
            out[f"{d.name} (unreadable)"] = 0
            continue
        if n:
            out[d.name] = n
    return out


def slots(plan: Plan, doc: dict[str, Any], cfg: Config) -> tuple[int, int]:
    """(live workers counted against this plan's launches, the cap they are counted against).

    Both are machine-wide: workers in other plans count. A plan's own `max_workers` can only
    lower what this plan may run, so the cap returned is the tighter of the machine's and
    (other plans' workers + this plan's own limit).
    """
    own = len(_busy(doc["items"], graph.states(plan, doc)))
    others = sum(busy_elsewhere(plan).values())
    cap = cfg.max_workers
    if plan.max_workers:
        cap = min(cap, others + plan.max_workers)
    return own + others, cap


def admit(cfg: Config, world: World) -> tuple[bool | None, str]:
    """Ask the configured admission command whether the box can take one more worker.

    True admit; False not now, with the reason; None could not look -- the count cap alone
    then applies, and the reason says so. No command configured: True.
    """
    if not cfg.admission:
        return True, ""
    try:
        rc, out, err = world.run(list(cfg.admission))
    except (OSError, Exception) as e:  # noqa: BLE001 -- a missing command is "could not look"
        return None, f"admission check could not run: {e}"
    text = (out or err).strip()
    if rc == 0:
        return True, text
    if rc == 75:
        return False, text.removeprefix("not now: ")
    return None, f"admission check failed (exit {rc}): {text}"


def prepare(plan: Plan, plan_dir: Path, cfg: Config, world: World, ids: list[str],
            relaunch: bool = False) -> list[launch_mod.Launch]:
    """The launches that would happen for `ids`, refusing anything the rules forbid."""
    doc = store.read(plan_dir)
    sts = graph.states(plan, doc)
    used, cap = slots(plan, doc, cfg)
    if not relaunch:
        ok, why = admit(cfg, world)
        if ok is False:
            raise EngineError(f"the box is busy, so launches wait: {why}")
    out = []
    for iid in ids:
        it = plan.items.get(iid)
        if it is None:
            raise EngineError(f"no item {iid!r} in plan {plan.name!r}.")
        if it.kind != "build":
            raise EngineError(f"{iid} is a {it.kind} item; only build items get a worker. "
                              + ("Decisions are the user's." if it.kind == "decision" else
                                 "External items wait on the world; zamu only polls them."))
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
        if it.engine not in cfg.engines:
            raise EngineError(f"{iid} wants engine {it.engine!r}, which config does not define; "
                              f"known: {', '.join(sorted(cfg.engines))}. Add it under [engines].")
        if relaunch and it.engine != "claude":
            session = None  # only claude can resume a conversation; others start over
        out.append(launch_mod.plan_launch(
            plan, it, tmux_session=cfg.tmux_session or world.tmux_session(),
            claude=cfg.engines[it.engine], engine=it.engine,
            default_branch=default, worktree_exists=exists,
            branch_exists=False if exists else launch_mod.branch_exists(it.repo, it.topic, world.run),
            session=session, worktree=kept))
    return out


def ready_to_fill(plan: Plan, plan_dir: Path, cfg: Config,
                  world: World | None = None) -> list[str]:
    """Ready build items that fit the free slots. With an admission command, at most one, and
    only if the box admits it: a launch does not load the box until the worker gets going, so
    filling every slot on one reading would admit them all against the same idle moment."""
    doc = store.read(plan_dir)
    used, cap = slots(plan, doc, cfg)
    ready = graph.launchable(plan, graph.states(plan, doc))[:max(cap - used, 0)]
    if ready and cfg.admission and world is not None:
        ok, _why = admit(cfg, world)
        return [] if ok is False else ready[:1]
    return ready


def commit_launch(plan: Plan, plan_dir: Path, cfg: Config, world: World,
                  ln: launch_mod.Launch) -> dict[str, str]:
    """Execute one launch and record it. The cap is re-checked under the lock."""
    now = world.now()
    from . import config
    # Two PMs launching at once could each see the last free slot. The machine lock makes the
    # check-then-launch atomic across plans; the plan lock below still guards this plan's state.
    with store.locked(config.home(), wait=120), store.locked(plan_dir):
        doc = store.read(plan_dir)
        used, cap = slots(plan, doc, cfg)
        if not ln.resume and used >= cap:
            raise EngineError(f"{used} of {cap} workers are live; {ln.item} waits for a slot.")
        got = launch_mod.execute(ln, world.run)
        rec = _rec(doc, ln.item)
        engine = plan.items[ln.item].engine
        rec.update({"state": rec.get("state") if ln.resume or rec.get("state") in graph.ACTIVE
                    else "running",
                    "session": ln.session if engine == "claude" else ln.token,
                    "pane": got.get("pane", ""), "ours": True, "wid": rec.get("wid") or ln.item,
                    "worktree": ln.worktree, "launched": _iso(now), "last_report": _iso(now),
                    "worker": "starting"})
        if engine != "claude":
            rec["session"] = ""
            rec["token"] = ln.token
        _register_worker(doc, rec["wid"], engine, rec.get("session", ""), rec["pane"],
                         ln.worktree, ln.item, ours=True, token=ln.token)
        rec["last_line"] = "relaunched" if ln.resume else "launched"
        store.write(plan_dir, doc)
        _event(plan_dir, now, ln.item, "relaunch" if ln.resume else "launch",
               session=ln.session, pane=got.get("pane", ""), worktree=ln.worktree)
    return got


def _register_worker(doc: dict[str, Any], wid: str, engine: str, session: str, pane: str,
                     worktree: str, item: str = "", ours: bool = False, token: str = "",
                     note: str = "") -> dict[str, Any]:
    w = doc.setdefault("workers", {}).setdefault(wid, {"items": []})
    w.update({k: v for k, v in {"engine": engine, "session": session, "pane": pane,
                                "worktree": worktree, "note": note, "token": token}.items() if v})
    if ours:
        w["ours"] = True
    if item and item not in w["items"]:
        w["items"].append(item)
    return w


def worker_add(plan_dir: Path, world: World, wid: str, engine: str, session: str = "",
               pane: str = "", worktree: str = "", note: str = "") -> str:
    """Register an agent someone else started -- a Claude session, a Codex window, a subagent
    the PM runs -- so items can be assigned to it and zamu can watch what it can."""
    if not wid or not wid.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise EngineError(f"worker id {wid!r}: use letters, digits, '-', '_' and '.'.")
    with store.transaction(plan_dir) as doc:
        _register_worker(doc, wid, engine, session, pane, worktree, note=note)
        _event(plan_dir, world.now(), "", "worker-add", wid=wid, engine=engine, session=session,
               pane=pane, worktree=worktree)
    proofs = ("session-coord's board" if session else "") + (" and " if session and pane else "") \
        + ("its pane" if pane else "")
    return f"worker {wid} ({engine}) registered; liveness from {proofs or 'reports only'}"


def assign(plan: Plan, plan_dir: Path, world: World, iid: str, wid: str, note: str = "") -> str:
    """Give an item to a registered worker. zamu records it; the PM delivers the brief,
    because how to reach a worker depends on what it is."""
    it = plan.items.get(iid)
    if it is None:
        raise EngineError(f"no item {iid!r} in plan {plan.name!r}.")
    if it.kind != "build":
        raise EngineError(f"{iid} is a {it.kind} item; only build items have workers.")
    now = world.now()
    with store.transaction(plan_dir) as doc:
        w = doc.get("workers", {}).get(wid)
        if w is None:
            raise EngineError(f"no worker {wid!r}; register it with `zamu worker add`.")
        sts = graph.states(plan, doc)
        rec = _rec(doc, iid)
        if sts[iid] in graph.FINAL:
            raise EngineError(f"{iid} is already {sts[iid]}.")
        if rec.get("wid") and rec["wid"] != wid and rec.get("worker") != "dead":
            raise EngineError(f"{iid} already has a worker ({rec['wid']}, "
                              f"{rec.get('worker', 'unchecked')}); `zamu mark {iid} reset` "
                              "first if that one is gone.")
        if (rec.get("session") and not rec.get("wid") and rec["session"] != w.get("session")
                and rec.get("worker") != "dead"):
            raise EngineError(f"{iid} already has a worker ({rec['session']}); "
                              f"`zamu mark {iid} reset` first if that one is gone.")
        rec.update({"state": rec.get("state") if sts[iid] in graph.ACTIVE else "running",
                    "wid": wid, "session": w.get("session", ""), "pane": w.get("pane", ""),
                    "worktree": w.get("worktree") or f"{it.repo.rstrip('/')}/.worktrees/{it.topic}",
                    "launched": _iso(now), "last_report": _iso(now), "worker": "starting",
                    "last_line": note or f"assigned to {wid}"})
        if w.get("ours"):
            rec["ours"] = True
        rec.pop("reopened", None)
        if iid not in w["items"]:
            w["items"].append(iid)
        _event(plan_dir, now, iid, "assign", wid=wid, note=note)
    msg = f"{iid} assigned to {wid} ({w.get('engine', '?')})"
    if not Path(rec["worktree"]).is_dir():
        msg += (f"\nwarning: {rec['worktree']} does not exist yet. A relaunch would create it "
                "from origin; register the worker again with --worktree once it has moved.")
    return msg


def adopt(plan: Plan, plan_dir: Path, world: World, iid: str, session: str, pane: str,
          worktree: str, note: str, engine: str = "claude", wid: str = "") -> str:
    """Register a worker someone started by hand and assign it this item, in one step.

    Nothing is launched. The cap is not enforced, because the worker already exists; refusing
    would only make the record disagree with the world.
    """
    it = plan.items.get(iid)
    if it is None:
        raise EngineError(f"no item {iid!r} in plan {plan.name!r}.")
    if it.kind != "build":
        raise EngineError(f"{iid} is a {it.kind} item; only build items have workers.")
    wid = wid or iid
    wt = worktree or f"{it.repo.rstrip('/')}/.worktrees/{it.topic}"
    doc = store.read(plan_dir)
    rec = doc["items"].get(iid, {})
    cur = rec.get("wid") or (iid if rec.get("session") else "")
    if cur and rec.get("session") != session and rec.get("worker") != "dead" \
            and graph.states(plan, doc)[iid] in graph.ACTIVE:
        raise EngineError(f"{iid} already has a worker ({rec.get('session') or cur}, "
                          f"{rec.get('worker', 'unchecked')}); `zamu mark {iid} reset` first "
                          "if that one is gone.")
    worker_add(plan_dir, world, wid, engine, session, pane, wt, note)
    out = assign(plan, plan_dir, world, iid, wid, note or "adopted")
    return out.replace(f"{iid} assigned to", f"{iid} adopted: worker", 1) + \
        (f", session {session}" if session else "") + (f", pane {pane}" if pane else "")


# --- report, mark, attach -----------------------------------------------------------------

def report(plan: Plan, plan_dir: Path, world: World, iid: str, state: str, line: str,
           evidence: str = "") -> str:
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
                rec.pop("reopened", None)
                rec.pop("stale", None)
                outcome = f"{iid} done; oracle agrees ({verdict.detail})" + _ripple(plan, doc, iid)
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
        if evidence:
            _add_evidence(doc, rec, evidence, "worker report", now)
        _event(plan_dir, now, iid, "report", state=state, line=line, outcome=outcome,
               **({"evidence": evidence} if evidence else {}))
        pm = dict(doc.get("pm", {}))
    from_pm = bool(pm.get("pane")) and pm.get("pane") == world.pane()
    if state in ("review", "done") + FLAGS and pm.get("repo") and pm.get("target") and not from_pm:
        world.coord.msg(pm["repo"], pm["target"],
                        f"zamu: {outcome}. Worker says: {line}. Run `zamu pulse --plan "
                        f"{plan.name}`.")
    return outcome


def mark(plan: Plan, plan_dir: Path, world: World, iid: str, state: str, note: str,
         evidence: str = "") -> str:
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
            rec.pop("reopened", None)
            rec.pop("stale", None)
        rec = doc["items"][iid]
        rec["note"] = note
        if evidence:
            _add_evidence(doc, rec, evidence, "mark", now)
        _event(plan_dir, now, iid, "mark", state=state, note=note)
        ripple = _ripple(plan, doc, iid) if state == "done" else ""
    return f"{iid} marked {state}{ripple}"


def verify(plan: Plan, plan_dir: Path, world: World, iid: str, ok: str = "",
           fail: str = "") -> str:
    """Re-establish a done item's evidence, typically after a pin it rests on moved.

    With neither flag, the oracle runs now. `--ok <evidence>` records a check done elsewhere --
    an expensive falsifier a worker ran -- and `--fail <why>` reopens the item. A reopened item
    is not closed again by its oracle alone, since the oracle may be what failed to notice: it
    needs a worker's `done` or another `verify`.
    """
    it = plan.items.get(iid)
    if it is None:
        raise EngineError(f"no item {iid!r} in plan {plan.name!r}.")
    now = world.now()
    verdict = None
    if not ok and not fail:
        if not it.oracle or oracles.is_manual(it.oracle):
            raise EngineError(f"{iid}'s oracle is manual; run the check and record it with "
                              "--ok <evidence> or --fail <why>.")
        cwd = it.repo if it.repo and Path(it.repo).is_dir() else str(plan_dir)
        verdict = oracles.check(it.oracle, cwd, world.oracle_run)
        if verdict.ok is None:
            return f"{iid}: could not check ({verdict.detail}); nothing changed"
    with store.transaction(plan_dir) as doc:
        rec = _rec(doc, iid)
        good = bool(ok) or (verdict is not None and verdict.ok is True)
        why = fail or (verdict.detail if verdict else "")
        if good:
            rec.pop("stale", None)
            rec.pop("reopened", None)
            rec["state"] = "done"
            _add_evidence(doc, rec, ok or verdict.detail, "verify", now)
            outcome = f"{iid} verified at pins {_pin_snapshot(doc) or '{}'}"
        else:
            for k in ("state", "stale", "claimed_done"):
                rec.pop(k, None)
            rec["reopened"] = f"{now.date()}: {why}"
            outcome = f"{iid} reopened: {why}"
        _event(plan_dir, now, iid, "verify", ok=good, detail=ok or why)
    return outcome


def attach(plan_dir: Path, world: World, repo: str, target: str, pane: str = "",
           tmux_session: str = "") -> str:
    """Record where the PM session is, so worker reports and the watcher can wake it through
    session-coord, and so the watcher opens worker windows in the PM's tmux session."""
    with store.transaction(plan_dir) as doc:
        doc["pm"] = {"repo": repo, "target": target, "pane": pane,
                     "tmux_session": tmux_session, "since": _iso(world.now())}
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
    roots = world.pane_pids()
    found = procs_mod.analyse(ps, trees(plan, doc, sts), doc.get("procs", {}),
                              world.now().timestamp(), cfg.idle_minutes, own_pids=(os.getpid(),),
                              protected=None if roots is None else procs_mod.protected_pids(ps, roots))
    out = []
    for f in found:
        line = (f"pid {f.proc.pid} [{f.item or 'no item'}] {', '.join(f.why)}: "
                f"{f.proc.cmd[:80]}  (cwd {f.proc.cwd})")
        if not f.reapable:
            why = ("tmux could not be asked which processes are live sessions" if roots is None
                   else "it is live, in a pane, or not orphaned; tell whoever owns it")
            out.append(f"leave  {line} -- {why}")
        elif go:
            out.append(f"{kill(f.proc)}  {line}")
        else:
            out.append(f"would kill  {line}")
    return out
