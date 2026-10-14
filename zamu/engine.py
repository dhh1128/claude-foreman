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
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from . import budgets, graph, launch as launch_mod, oracles, procs as procs_mod, store
from .config import Config, ConfigError, ModelSpec
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
    #: Whether a pane id exists, or None when tmux could not be asked. Existence is not ownership.
    pane_exists: Callable[[str], "bool | None"] = lambda pane: None
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
    pane_pid: Callable[[str], "int | None"] = lambda pane: None
    merge_locked: Callable[[str], "bool | None"] | None = None
    codex_session: Callable[[str, str], "str | None"] = lambda tree, since: None
    budget_run: Callable[[list[str]], tuple[int, str, str]] | None = None


class EngineError(RuntimeError):
    """A verb refused. The message says why and what to do instead."""


class BusyError(EngineError):
    """Admission refused a launch; a watcher may retry silently next tick."""


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


ASK_IDS = re.compile(r"\b[AQ]-[A-Z0-9]+\b")


def _pane_tree(world: World, pane: str,
               ps: list[procs_mod.Proc] | None) -> list[procs_mod.Proc] | None:
    root = world.pane_pid(pane)
    if root is None or ps is None:
        return None
    by_parent: dict[int, list[procs_mod.Proc]] = {}
    for p in ps:
        by_parent.setdefault(p.ppid, []).append(p)
    out, todo, seen = [], [root], set()
    while todo:
        pid = todo.pop()
        if pid in seen:
            continue
        seen.add(pid)
        p = next((p for p in ps if p.pid == pid), None)
        if p is not None:
            out.append(p)
        todo.extend(p.pid for p in by_parent.get(pid, []))
    return out if out and out[0].pid == root else None


def park_due(plan: Plan, plan_dir: Path, cfg: Config, world: World) -> list[str]:
    """Park proved idle windows after a blocked report or sustained quiet pane and CPU."""
    now = world.now()
    changes = []
    with store.locked(plan_dir):
        doc = store.read(plan_dir)
        processes: list[procs_mod.Proc] | None = None
        scanned = False
        for iid, it in plan.items.items():
            rec = doc.get("items", {}).get(iid, {})
            if graph.states(plan, doc)[iid] in graph.ACTIVE and not rec.get("ours"):
                last = _parse(rec.get("last_report", ""))
                if ((rec.get("waiting") or rec.get("stuck")) and last and
                        (now - last).total_seconds() >= cfg.park_after_minutes * 60):
                    rec["park_refusal"] = "window ownership and resume path are not proven"
                else:
                    rec.pop("park_refusal", None)
                continue
            if (graph.states(plan, doc)[iid] not in graph.ACTIVE or
                    rec.get("worker") not in ("live", "starting") or not rec.get("ours") or
                    not rec.get("pane") or not rec.get("worktree")):
                continue
            rec.pop("park_refusal", None)
            pane = rec["pane"]
            identity = rec.get("token") or rec.get("session")
            if not identity or world.pane_has_session(pane, identity) is not True:
                continue
            if not scanned:
                processes = world.scan()
                scanned = True
            tree = _pane_tree(world, pane, processes)
            activity = world.pane_activity(pane)
            if tree is None or activity is None:
                continue
            # The root shell and engine are expected. An extra process can be a merge/CI
            # tool waiting on the world; an idle CPU reading must never hide that work.
            signature = {p.key: p.cpu for p in tree}
            previous = rec.get("park_sample", {})
            since = (previous.get("since") if previous.get("cpu") == signature and
                     previous.get("last_report") == rec.get("last_report") else _iso(now))
            rec["park_sample"] = {"cpu": signature, "since": since,
                                  "last_report": rec.get("last_report")}
            if len(tree) > 2:
                continue
            stable = _parse(since)
            if stable is None or (now - stable).total_seconds() < cfg.park_after_minutes * 60:
                continue
            blocked = rec.get("waiting") or rec.get("stuck")
            last = _parse(rec.get("last_report", ""))
            reported_long = bool(blocked and last and
                                 (now - last).total_seconds() >= cfg.park_after_minutes * 60)
            quiet_long = (graph.states(plan, doc)[iid] == "running" and last and
                          (now - last).total_seconds() >= cfg.park_after_minutes * 60 and
                          now.timestamp() - activity >= cfg.park_after_minutes * 60)
            if not (reported_long or quiet_long):
                continue
            session = rec.get("session") if rec.get("engine", "claude") == "claude" else rec.get("codex_session")
            if rec.get("engine") == "codex" and not session:
                session = world.codex_session(rec["worktree"], rec.get("launched", ""))
                if session:
                    rec["codex_session"] = session
            if not session or rec.get("engine", "claude") not in ("claude", "codex"):
                rec["park_refusal"] = "no proven resumable conversation; keep the worker live"
                continue
            rec.pop("park_refusal", None)
            locked = (world.merge_locked or world.coord.merge_locked)(it.repo)
            if locked is not False:
                continue
            try:
                rc, out, _err = world.run(["git", "-C", rec["worktree"], "status", "--porcelain",
                                           "--untracked-files=all"])
            except (OSError, subprocess.TimeoutExpired):
                continue
            if rc != 0:
                continue
            dirty = [line[3:] for line in out.splitlines() if len(line) >= 4]
            reason = blocked or f"pane quiet and process tree idle for {cfg.park_after_minutes:g}m"
            asks = sorted(set(ASK_IDS.findall(reason)))
            open_asks = world.coord.open_asks(it.repo) if asks else None
            try:
                rc, _out, _err = world.run(["tmux", "kill-window", "-t", pane])
            except (OSError, subprocess.TimeoutExpired):
                continue
            if rc != 0:
                continue
            prior_state = graph.states(plan, doc)[iid]
            rec["state"] = "parked"
            rec["worker"] = "parked"
            rec.pop("park_refusal", None)
            rec["parked"] = {"at": _iso(now), "reason": reason, "asks": asks,
                             "asks_seen": sorted(set(asks) & open_asks) if open_asks is not None else [],
                             "prior_state": prior_state,
                             "last_report": rec.get("last_report", ""),
                             "last_line": rec.get("last_line", ""), "dirty": dirty,
                             "session": session, "engine": rec.get("engine", "claude"),
                             "worktree": rec["worktree"]}
            wid = rec.get("wid")
            if wid in doc.get("workers", {}):
                doc["workers"][wid]["worker"] = "parked"
                doc["workers"][wid]["pane"] = ""
            rec["pane"] = ""
            changes.append(f"{iid} parked: {reason}")
            _event(plan_dir, now, iid, "park", reason=reason, asks=asks, dirty=dirty)
        store.write(plan_dir, doc)
    return changes


# --- pulse --------------------------------------------------------------------------------

def _needs_oracle(it: Item, st: str) -> bool:
    return st not in graph.FINAL and bool(it.oracle) and not oracles.is_manual(it.oracle)


@dataclass(frozen=True)
class ReportRead:
    identity: tuple[int, int] | None
    restarted: bool
    lines: tuple[tuple[int, str], ...]
    digest: str


def _report_lines(path: Path, offset: int, identity: list[int] | None,
                  digest: str) -> ReportRead | None:
    """Read complete lines, restarting if the consumed prefix was replaced or rewritten."""
    try:
        with path.open("rb") as f:
            stat = os.fstat(f.fileno())
            file_id = (stat.st_dev, stat.st_ino)
            data = f.read()
    except FileNotFoundError:
        return None if offset else ReportRead(None, False, (), "")
    except OSError:
        return None
    prefix_matches = offset <= len(data) and bool(digest) and \
        hashlib.sha256(data[:offset]).hexdigest() == digest
    restarted = offset > len(data) or (offset > 0 and bool(digest) and not prefix_matches) or \
        (offset > 0 and identity is not None and tuple(identity) != file_id and not prefix_matches)
    start = 0 if restarted else offset
    out = []
    pos = start
    for raw in data[start:].split(b"\n")[:-1]:
        pos += len(raw) + 1
        try:
            out.append((pos, raw.removesuffix(b"\r").decode("utf-8")))
        except UnicodeDecodeError:
            out.append((pos, ""))
    return ReportRead(file_id, restarted, tuple(out),
                      hashlib.sha256(data[:pos]).hexdigest())


def _parse_report_line(raw: str) -> tuple[dt.datetime, str, str] | None:
    parts = raw.split(" ", 2)
    if len(parts) != 3 or parts[1] not in REPORT_STATES or not parts[2].strip():
        return None
    when = _parse(parts[0])
    if when is None or when.tzinfo is None:
        return None
    return when, parts[1], parts[2]


def pulse(plan: Plan, plan_dir: Path, cfg: Config, world: World,
          oracles_due: bool = True, notify_parks: bool = True) -> dict[str, Any]:
    """Check oracles and workers, file escalations, and return what changed.

    Oracles and session-coord are consulted outside the lock, because a slow `gh` would
    otherwise hold every worker's `report` waiting. Their results are then applied in one
    transaction against a fresh read, so a report that landed meanwhile is not overwritten.
    """
    now = world.now()
    snap = store.read(plan_dir)
    sts = graph.states(plan, snap)

    file_reports: dict[str, tuple[str, str, int, list[int] | None, str, ReportRead | None,
                                  list[tuple[int, str, tuple | None, oracles.Verdict | None]]]] = {}
    for iid, it in plan.items.items():
        rec = snap["items"].get(iid, {})
        if sts[iid] not in graph.ACTIVE or not rec.get("worktree"):
            continue
        path = Path(rec["worktree"]) / ".zamu" / "reports"
        path_str = str(path)
        same_path = rec.get("report_path", path_str) == path_str
        offset = rec.get("report_offset", 0) if same_path else 0
        old_id = rec.get("report_identity") if same_path else None
        old_digest = rec.get("report_digest", "") if same_path else ""
        read = _report_lines(path, offset, old_id, old_digest)
        pending = []
        for end, raw in read.lines if read is not None else ():
            parsed = _parse_report_line(raw)
            verdict = None
            if parsed and parsed[1] == "done" and it.oracle and not oracles.is_manual(it.oracle):
                cwd = it.repo if it.repo and Path(it.repo).is_dir() else str(plan_dir)
                verdict = oracles.check(it.oracle, cwd, world.oracle_run)
            pending.append((end, raw, parsed, verdict))
        file_reports[iid] = rec["worktree"], path_str, offset, old_id, old_digest, read, pending

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

    idle_proofs = _idle_worker_proofs(plan, snap, world, now)
    budget_readings = _budget_readings(cfg, world) if cfg.budgets else {}

    changes: list[str] = []
    messages: list[tuple[str, str]] = []
    with store.transaction(plan_dir) as doc:
        for iid, (worktree, path, start, old_id, old_digest, read, pending) in file_reports.items():
            rec = _rec(doc, iid)
            if (graph.states(plan, doc)[iid] not in graph.ACTIVE or
                    rec.get("worktree") != worktree or
                    (rec.get("report_offset", 0) if rec.get("report_path", path) == path else 0) != start or
                    (rec.get("report_identity") if rec.get("report_path", path) == path else None) != old_id or
                    (rec.get("report_digest", "") if rec.get("report_path", path) == path else "") != old_digest):
                continue
            if rec.get("report_path") != path:
                rec["report_path"] = path
                rec["report_offset"] = 0
                rec.pop("report_identity", None)
                rec.pop("report_digest", None)
                rec.pop("report_file_errors", None)
            if read is None:
                rec["report_read_error"] = f"{path}: cannot read reports from byte {start}; inspect the file"
                continue
            rec.pop("report_read_error", None)
            if read.identity is not None:
                rec["report_identity"] = list(read.identity)
            if read.restarted:
                rec["report_offset"] = 0
                _event(plan_dir, now, iid, "report_file_restart", path=path)
            for end, raw, parsed, verdict in pending:
                if parsed is None:
                    reason = f"{path}: malformed report at byte {rec['report_offset']}: {raw[:100]!r}"
                    rec.setdefault("report_file_errors", []).append(reason)
                    _event(plan_dir, now, iid, "report_file_error", detail=reason)
                else:
                    when, state, line = parsed
                    outcome = _apply_report(plan, plan_dir, doc, iid, state, line, when, verdict)
                    changes.append(outcome)
                    if state in ("review", "done") + FLAGS:
                        messages.append((outcome, line))
                rec["report_offset"] = end
            if read.identity is not None:
                rec["report_digest"] = read.digest
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

        _check_idle_workers(plan, plan_dir, doc, idle_proofs, now)

        changes += _pins(plan, plan_dir, doc, pin_values, now)
        _ready_since(plan, doc, graph.states(plan, doc), now)
        _record_nags(plan, plan_dir, doc, now)
        if cfg.escalate == "session-coord":
            changes += _escalate(plan, plan_dir, doc, sts, world, now)
        _quiet_panes(plan, doc, sts, world)
        if cfg.admission and admission_can_block(plan, doc, cfg):
            ok, why = admit(cfg, world)
            doc["admission"] = {"ok": ok, "why": why, "at": _iso(now)}
        else:
            doc.pop("admission", None)
        if cfg.budgets:
            changes += _budget_episode(plan, plan_dir, doc, cfg, world, now, budget_readings)
        _record_launch_holds(plan, plan_dir, doc, cfg, budget_readings, now)
        changes += _leftovers(plan, plan_dir, doc, graph.states(plan, doc), cfg, world, now)
        pm = dict(doc.get("pm", {}))
    for outcome, line in messages:
        _message_pm(pm, world, plan.name, outcome, line)
    parked = park_due(plan, plan_dir, cfg, world)
    if notify_parks:
        for change in parked:
            _message_pm(pm, world, plan.name, change, change.partition(": ")[2])
    changes += parked
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
        in_pane = world.pane_exists(pane)  # presence proves liveness, not ownership
    if (sid and live is not None and sid in live) or in_pane:
        return "live"
    if in_pane is None and (not sid or live is None):
        return "unknown"
    launched = _parse(rec.get("launched", ""))
    if launched and now - launched < grace:
        return "starting"
    return "dead"


def _idle_worker_status(plan: Plan, w: dict[str, Any], world: World,
                        now: dt.datetime) -> str:
    """Ask the same liveness authorities for a worker after its last item has finished."""
    if w.get("worker") == "retired":
        return "retired"
    repo = next((plan.items[i].repo for i in w.get("items", []) if i in plan.items), "")
    live = world.coord.live_sessions(repo) if w.get("session") and repo else None
    return _liveness(w, live, world, now, dt.timedelta(0))


def _worker_identity(w: dict[str, Any]) -> tuple:
    return (w.get("session"), w.get("pane"), w.get("token"), w.get("ours"),
            tuple(w.get("items", [])))


def _idle_worker_proofs(plan: Plan, doc: dict[str, Any], world: World,
                        now: dt.datetime) -> dict[str, tuple[tuple, str]]:
    return {wid: (_worker_identity(w), _idle_worker_status(plan, w, world, now))
            for wid, w in doc.get("workers", {}).items()
            if w.get("worker") not in ("retired", "parked")}


def _check_idle_workers(plan: Plan, plan_dir: Path, doc: dict[str, Any],
                        proofs: dict[str, tuple[tuple, str]], now: dt.datetime) -> None:
    sts = graph.states(plan, doc)
    for wid, w in doc.get("workers", {}).items():
        if w.get("worker") == "parked":
            continue
        if any(sts.get(i) in graph.ACTIVE for i in w.get("items", [])):
            continue
        proof = proofs.get(wid)
        if proof is None or proof[0] != _worker_identity(w):
            continue
        status = proof[1]
        if status == "unknown" and w.get("worker") == "dead":
            continue  # an unreadable probe is not evidence that a dead worker returned
        if status != w.get("worker"):
            w["worker"] = status
            _event(plan_dir, now, "", "worker", wid=wid, worker=status)


def refresh_idle_workers(plan: Plan, plan_dir: Path, world: World) -> None:
    """Refresh the registry before a read-only board or worker listing offers reuse."""
    now = world.now()
    proofs = _idle_worker_proofs(plan, store.read(plan_dir), world, now)
    with store.transaction(plan_dir) as doc:
        _check_idle_workers(plan, plan_dir, doc, proofs, now)


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


def worker_identities(plan: Plan, plan_dir: Path, doc: dict[str, Any],
                      sts: dict[str, str]) -> dict[str, tuple[str, str, str]]:
    """Map explicit process tokens and session ids to their launching plan and item."""
    from . import config
    result = {}
    dirs = [plan_dir] + [config.plans_dir() / n for n in config.known_plans()
                         if (config.plans_dir() / n).resolve() != plan_dir.resolve()]
    for d in dirs:
        state = doc if d == plan_dir else store.read(d)
        for iid, rec in state.get("items", {}).items():
            for ident in (rec.get("session"), rec.get("token"), rec.get("wid")):
                if ident:
                    status = sts.get(iid, "") if d == plan_dir else rec.get("state", "")
                    result[ident] = (plan.name if d == plan_dir else d.name, iid, status)
    return result


def _leftovers(plan: Plan, plan_dir: Path, doc: dict[str, Any], sts: dict[str, str], cfg: Config,
               world: World, now: dt.datetime) -> list[str]:
    ps = world.scan()
    if ps is None:
        return []  # could not look; keep the last findings rather than clearing them
    t = trees(plan, doc, sts)
    epoch = now.timestamp()
    roots = world.pane_pids()
    found = procs_mod.analyse(ps, t, doc.get("procs", {}), epoch, cfg.idle_minutes,
                              own_pids=(os.getpid(),),
                              protected=None if roots is None else procs_mod.protected_pids(ps, roots),
                              identities=worker_identities(plan, plan_dir, doc, sts),
                              plan_name=plan.name)
    doc["procs"] = procs_mod.next_cpu_record(ps, doc.get("procs", {}), epoch, t)
    before = {f["key"] for f in doc.get("leftovers", [])}
    doc["leftovers"] = [{"key": f.proc.key, "pid": f.proc.pid, "item": f.item,
                         "why": list(f.why), "cwd": f.proc.cwd, "cmd": f.proc.cmd[:120],
                         "reapable": f.reapable} for f in found]
    new = [f for f in found if f.proc.key not in before]
    return [(f"live worker in a deleted worktree pid {f.proc.pid} ({f.item})"
             if "live worker in a deleted worktree" in f.why else
             f"leftover pid {f.proc.pid} ({f.item or 'no item'}): {', '.join(f.why)}")
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


def _record_nags(plan: Plan, plan_dir: Path, doc: dict[str, Any], now: dt.datetime) -> None:
    sts = graph.states(plan, doc)
    for iid, it in plan.items.items():
        rec = doc["items"].get(iid, {})
        since = rec.get("ready_since", "")
        if sts[iid] in graph.FINAL or not it.nag_after_days or not since:
            rec.pop("nagged_for", None)
        elif waited_days(doc, iid, now) > it.nag_after_days and rec.get("nagged_for") != since:
            rec["nagged_for"] = since
            _event(plan_dir, now, iid, "wait_nag", ready_since=since)


def _record_launch_holds(plan: Plan, plan_dir: Path, doc: dict[str, Any], cfg: Config,
                         readings: dict, now: dt.datetime) -> None:
    ready = set(graph.launchable(plan, graph.states(plan, doc)))
    wanted = set()
    if doc.get("admission", {}).get("ok") is False:
        load = engine_load(plan, doc)
        wanted.update((iid, "admission") for iid in ready
                      if any(e in cfg.engines and (e not in cfg.engine_caps or
                             load.get(e, 0) < cfg.engine_caps[e])
                             for e in plan.items[iid].engines))
    wanted.update((iid, "budget") for iid in budget_waiting(
        plan, doc, cfg, readings, ignore_admission=True))
    prior = {(iid, reason) for iid, reasons in doc.get("friction_holds", {}).items()
             for reason in reasons}
    for iid, reason in sorted(wanted - prior):
        _event(plan_dir, now, iid, "launch_hold", reason=reason, state="start")
    for iid, reason in sorted(prior - wanted):
        _event(plan_dir, now, iid, "launch_hold", reason=reason, state="end")
    holds: dict[str, list[str]] = {}
    for iid, reason in sorted(wanted):
        holds.setdefault(iid, []).append(reason)
    if holds:
        doc["friction_holds"] = holds
    else:
        doc.pop("friction_holds", None)


def budget_waiting(plan: Plan, doc: dict[str, Any], cfg: Config, readings: dict,
                   *, ignore_admission: bool = False) -> list[str]:
    """Items held by budget, with admission optionally ignored for friction accounting."""
    if not ignore_admission and doc.get("admission", {}).get("ok") is False:
        return []
    used, cap = slots(plan, doc, cfg)
    if used >= cap:
        return []
    load = engine_load(plan, doc)
    return [iid for iid in graph.launchable(plan, graph.states(plan, doc))
            if all(e in cfg.engines and e in cfg.budgets and
                   (e not in cfg.engine_caps or load.get(e, 0) < cfg.engine_caps[e]) and
                   readings.get(e, {}).get("verdict") == "not-now"
                   for e in plan.items[iid].engines)]


def _budget_episode(plan: Plan, plan_dir: Path, doc: dict[str, Any], cfg: Config,
                    world: World, now: dt.datetime, readings: dict) -> list[str]:
    waiting = budget_waiting(plan, doc, cfg, readings)
    episode = doc.setdefault("budget_episode", {})
    recovered = any(readings.get(e, {}).get("verdict") == "admit"
                    for iid in episode.get("waiting", []) if iid in plan.items
                    for e in plan.items[iid].engines)
    resets = [_parse(readings[e].get("data", {}).get("resets_at", ""))
              for iid in waiting for e in plan.items[iid].engines]
    earliest = min((r for r in resets if r and r > now), default=None)
    if recovered or (episode.get("reset") and _parse(episode["reset"])
                       and now >= _parse(episode["reset"])):
        if episode.get("since") or episode.get("ask"):
            doc["budget_episode"] = {"number": episode.get("number", 0) + 1}
        episode = doc["budget_episode"]
    item_since = episode.setdefault("item_since", {})
    states = graph.states(plan, doc)
    for iid in list(item_since):
        if states.get(iid) != "ready":
            item_since.pop(iid)
    if not waiting:
        return []
    episode["waiting"] = waiting
    episode.setdefault("since", _iso(now))
    for iid in waiting:
        item_since.setdefault(iid, _iso(now))
    prior_reset = _parse(episode.get("reset", ""))
    if earliest and (prior_reset is None or earliest < prior_reset):
        episode["reset"] = _iso(earliest)
    due = any((now - _parse(item_since[iid])).total_seconds() >= cfg.budget_ask_minutes * 60
              for iid in waiting)
    if episode.get("ask") or not due:
        return []
    def figure(e: str) -> str:
        data = readings[e].get("data", {})
        shown = data.get("figure") or (f"{data['used_pct']}%" if data.get("used_pct") is not None
                                        else "not now")
        return f"{e}: {shown}" + (f", resets {data['resets_at']}" if data.get("resets_at") else "")

    figures = "; ".join(figure(e) for e in dict.fromkeys(
        e for iid in waiting for e in plan.items[iid].engines))
    items = "; ".join(f"{iid} ({', '.join(plan.items[iid].engines)})" for iid in waiting)
    text = (f"Plan {plan.name} waits for engine budget: {items}. {figures}. Choose whether to "
            f"{'wait until ' + earliest.isoformat() if earliest else 'wait for capacity'}, add an engine to "
            "these items, raise a probe threshold, or buy capacity.")
    episode["line"] = text
    if cfg.escalate == "none":
        episode["ask"] = "board"
        episode.pop("error", None)
        return []
    repo = doc.get("pm", {}).get("repo") or world.cwd()
    if not repo:
        return []
    tag = ask_tag(plan, f"budget-{episode.get('number', 0)}", "Q")
    ok, note = world.coord.ask_add(repo, tag, "Q", text)
    if ok:
        episode["ask"] = tag
        episode.pop("error", None)
        _event(plan_dir, now, "", "budget_escalate", tag=tag, repo=repo)
        return [f"budget trade-off escalated as {tag}"]
    episode["error"] = note
    return [f"budget escalation failed: {note}"]


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


def engine_load(plan: Plan, doc: dict[str, Any]) -> dict[str, int]:
    """Live workers per engine across every plan: this plan's from `doc`, others' from disk."""
    from . import config
    counts: dict[str, int] = {}

    def add(items: dict[str, Any], engines: dict[str, str]) -> None:
        seen = set()
        for iid, rec in items.items():
            if (rec.get("state") in graph.ACTIVE and (rec.get("session") or rec.get("pane")
                                                       or rec.get("wid"))
                    and rec.get("worker") != "dead"):
                w = rec.get("wid") or iid
                if w not in seen:
                    seen.add(w)
                    e = rec.get("engine") or engines.get(iid, "claude")
                    counts[e] = counts.get(e, 0) + 1

    add(doc.get("items", {}), {i: it.engines[0] for i, it in plan.items.items()})
    here = plan.path.parent.resolve()
    root = config.plans_dir()
    if root.is_dir():
        for d in root.iterdir():
            try:
                if d.resolve() == here or not (d / "state.json").exists():
                    continue
                add(store.read(d).get("items", {}), {})
            except (OSError, store.StoreError):
                continue
    return counts


def engine_holders(plan: Plan, doc: dict[str, Any], name: str) -> list[str]:
    """The live item identities consuming one engine cap, including other plans."""
    from . import config
    holders = []

    def add(items, prefix):
        seen = set()
        for iid, rec in items.items():
            wid = rec.get('wid') or iid
            if (wid not in seen and rec.get('state') in graph.ACTIVE
                    and rec.get('worker') != 'dead'
                    and (rec.get('session') or rec.get('pane') or rec.get('wid'))
                    and rec.get('engine', plan.items[iid].engines[0] if not prefix and iid in plan.items else 'claude') == name):
                holders.append(prefix + iid)
                seen.add(wid)

    add(doc.get('items', {}), '')
    root = config.plans_dir()
    if root.is_dir():
        for path in root.iterdir():
            try:
                if path.resolve() != plan.path.parent.resolve():
                    add(store.read(path).get('items', {}), path.name + '/')
            except (OSError, store.StoreError):
                continue
    return holders


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


def admission_can_block(plan: Plan, doc: dict[str, Any], cfg: Config) -> bool:
    """A ready item has room under both the machine and its engine cap."""
    used, cap = slots(plan, doc, cfg)
    if used >= cap:
        return False
    load = engine_load(plan, doc) if cfg.engine_caps else {}
    return any(any(e in cfg.engines and (e not in cfg.engine_caps
                   or load.get(e, 0) < cfg.engine_caps[e]) for e in plan.items[i].engines)
               for i in graph.launchable(plan, graph.states(plan, doc)))


def _budget_readings(cfg: Config, world: World, *, force: bool = False,
                     names: tuple[str, ...] | None = None, locked: bool = False) -> dict:
    if not cfg.budgets:
        return {}
    return budgets.refresh(cfg, world.now(), world.budget_run or world.run,
                           force=force, names=names, locked=locked)


def _choose(it: Item, cfg: Config, load: dict[str, int], readings: dict) -> tuple[str | None, str]:
    return budgets.choice(it.engines, cfg, load, readings)


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
            relaunch: bool = False, parked: bool = False,
            unblocked: str = "") -> list[launch_mod.Launch]:
    """The launches that would happen for `ids`, refusing anything the rules forbid."""
    doc = store.read(plan_dir)
    sts = graph.states(plan, doc)
    used, cap = slots(plan, doc, cfg)
    if not relaunch:
        ok, why = admit(cfg, world)
        if ok is False:
            raise BusyError(f"the box is busy, so launches wait: {why}")
    names = tuple(dict.fromkeys(e for iid in ids if iid in plan.items
                                for e in plan.items[iid].engines))
    readings = _budget_readings(cfg, world, names=names) if not relaunch else {}
    load = engine_load(plan, doc)
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
        changed = ""
        if parked:
            info = rec.get("parked", {})
            if sts[iid] != "parked" or not info.get("session"):
                raise EngineError(f"{iid} has no parked conversation to resume.")
            asks = info.get("asks", [])
            if not unblocked:
                if not asks:
                    raise EngineError(f"{iid} has no named asks; the PM must pass --unblocked WHY.")
                if not set(asks) <= set(info.get("asks_seen", [])):
                    raise EngineError(f"{iid}'s named asks were not observed open; pass --unblocked WHY after checking the wait.")
                open_asks = world.coord.open_asks(it.repo)
                if open_asks is None:
                    raise EngineError(f"could not read the ask queue for {iid}; pass --unblocked WHY after checking it.")
                held = set(asks) & open_asks
                if held:
                    raise EngineError(f"{iid} still waits on {', '.join(sorted(held))}.")
            changed = unblocked or f"asks {', '.join(asks)} are no longer open"
            session = info["session"]
            if used >= cap:
                raise EngineError(f"{used} of {cap} workers are live; {iid} waits for a slot.")
            used += 1
        elif relaunch:
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
        selected = ((rec.get("engine") or it.engines[0]) if relaunch or parked else
                    _choose(it, cfg, load, readings)[0])
        if parked and selected:
            chosen, why = budgets.choice((selected,), cfg, load, readings)
            if chosen is None:
                raise BusyError(f"{iid} waits to resume: {why}")
        if not selected:
            _chosen, reason = _choose(it, cfg, load, readings)
            if len(it.engines) == 1 and it.engines[0] not in cfg.engines:
                raise EngineError(f"{iid} wants engine {it.engines[0]!r}, which config does not define; "
                                  f"known: {', '.join(sorted(cfg.engines))}. Add it under [engines].")
            caps = []
            for e in it.engines:
                ecap = cfg.engine_caps.get(e)
                if ecap is None or load.get(e, 0) < ecap:
                    continue
                holders = [h if "/" in h else f"{plan.name}/{h}"
                           for h in engine_holders(plan, doc, e)]
                caps.append(f"{e} cap {ecap} held by {', '.join(holders) or 'no worker (cap is zero)'}")
            if len(it.engines) == 1 and caps:
                raise EngineError(f"{iid} waits: {caps[0]} (or set engine = \"<another>\" on it).")
            raise EngineError(f"{iid} waits for an engine: {reason}"
                              + (f"; {'; '.join(caps)}" if caps else ""))
        if not relaunch:
            load[selected] = load.get(selected, 0) + 1
        kept = rec.get("worktree", "") if relaunch or parked else ""
        if parked and not launch_mod.worktree_exists(kept):
            raise EngineError(f"{iid}'s parked worktree {kept} is missing; refusing to recreate it.")
        if kept and not launch_mod.worktree_exists(kept):
            kept = ""
        wt = kept or f"{it.repo.rstrip('/')}/.worktrees/{it.topic}"
        exists = launch_mod.worktree_exists(wt)
        default = it.base or ("" if exists else launch_mod.default_branch(it.repo, world.run))
        if selected not in cfg.engines:
            raise EngineError(f"{iid} wants engine {selected!r}, which config does not define; "
                              f"known: {', '.join(sorted(cfg.engines))}. Add it under [engines].")
        if relaunch and selected != "claude":
            session = None  # only claude can resume a conversation; others start over
        # An older launch has no model fields: it started on the engine default and must
        # resume there even if the item acquired a tier while dead or parked.
        spec = (ModelSpec(rec.get("model", ""), rec.get("reasoning_effort", ""))
                if relaunch or parked else
                cfg.model_for(selected, it.tier, it.model))
        out.append(launch_mod.plan_launch(
            plan, it, tmux_session=cfg.tmux_session or world.tmux_session(),
            claude=cfg.engines[selected], engine=selected,
            default_branch=default, worktree_exists=exists,
            branch_exists=False if exists else launch_mod.branch_exists(it.repo, it.topic, world.run),
            session=session, worktree=kept, changed=changed, parked=parked,
            model=spec.model, reasoning_effort=spec.reasoning_effort,
            tier=rec.get("tier", "") if relaunch or parked else it.tier))
    return out


def ready_to_resume(plan: Plan, plan_dir: Path, cfg: Config, world: World) -> list[str]:
    """Parked items whose named asks have all left the queue, subject to a free slot."""
    doc = store.read(plan_dir)
    used, cap = slots(plan, doc, cfg)
    if used >= cap:
        return []
    out = []
    for iid, it in plan.items.items():
        rec = doc.get("items", {}).get(iid, {})
        info = rec.get("parked", {})
        if (graph.states(plan, doc)[iid] != "parked" or not info.get("asks") or
                not set(info["asks"]) <= set(info.get("asks_seen", []))):
            continue
        asks = world.coord.open_asks(it.repo)
        if asks is not None and not set(rec["parked"]["asks"]) & asks:
            out.append(iid)
    return out[:max(cap - used, 0)]


def ready_to_fill(plan: Plan, plan_dir: Path, cfg: Config,
                  world: World | None = None, *, all_candidates: bool = False) -> list[str]:
    """Ready build items that fit the free slots. With an admission command, at most one, and
    only if the box admits it: a launch does not load the box until the worker gets going, so
    filling every slot on one reading would admit them all against the same idle moment."""
    doc = store.read(plan_dir)
    used, cap = slots(plan, doc, cfg)
    if used >= cap:
        return []
    load = engine_load(plan, doc)
    ready = [i for i in graph.launchable(plan, graph.states(plan, doc))
             if any(e in cfg.engines and (e not in cfg.engine_caps or
                    load.get(e, 0) < cfg.engine_caps[e])
                    for e in plan.items[i].engines)]
    if ready and cfg.admission and world is not None:
        ok, _why = admit(cfg, world)
        if ok is False:
            return []
    readings = _budget_readings(cfg, world) if world is not None and cfg.budgets else (budgets.read() or {})
    usable = []
    for iid in ready:
        it = plan.items[iid]
        chosen, _ = _choose(it, cfg, load, readings)
        if chosen:
            try:
                cfg.model_for(chosen, it.tier, it.model)
            except ConfigError:
                continue
            usable.append(iid)
    ready = usable
    ready.sort(key=lambda i: (_parse(doc['items'].get(i, {}).get('ready_since', ''))
                              or dt.datetime.max.replace(tzinfo=dt.timezone.utc), i))
    if all_candidates:
        return ready
    return ready[:1] if cfg.admission or cfg.budgets else ready[:max(cap - used, 0)]


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
        was_parked = doc.get("items", {}).get(ln.item, {}).get("state") == "parked"
        if ln.parked and (not was_parked or
                          doc["items"][ln.item].get("parked", {}).get("session") != ln.session):
            raise EngineError(f"{ln.item}'s parked conversation changed; prepare resume again.")
        if (not ln.resume or was_parked) and used >= cap:
            raise EngineError(f"{used} of {cap} workers are live; {ln.item} waits for a slot.")
        engine = ln.engine if not ln.resume else (doc.get("items", {}).get(ln.item, {}).get("engine")
                                                  or ln.engine)
        if not ln.resume or was_parked:
            readings = _budget_readings(cfg, world, force=True, names=plan.items[ln.item].engines,
                                        locked=True)
            chosen, why = (budgets.choice((engine,), cfg, engine_load(plan, doc), readings)
                           if was_parked else
                           _choose(plan.items[ln.item], cfg, engine_load(plan, doc), readings))
            if chosen != engine:
                raise BusyError(f"{ln.item} waits: engine choice changed ({why or chosen})")
        if was_parked:
            ok, why = admit(cfg, world)
            if ok is False:
                raise BusyError(f"the box is busy, so {ln.item} waits to resume: {why}")
        got = launch_mod.execute(ln, world.run)
        rec = _rec(doc, ln.item)
        rec["engine"] = engine
        rec["tier"] = ln.tier
        rec["model"] = ln.model
        rec["reasoning_effort"] = ln.reasoning_effort
        state = (rec.get("parked", {}).get("prior_state", "running") if was_parked else
                 rec.get("state") if ln.resume or rec.get("state") in graph.ACTIVE else "running")
        rec.update({"state": state,
                    "session": ln.session if engine == "claude" else ln.token,
                    "pane": got.get("pane", ""), "ours": True, "wid": rec.get("wid") or ln.item,
                    "worktree": ln.worktree, "launched": _iso(now), "last_report": _iso(now),
                    "worker": "starting"})
        if engine != "claude":
            rec["session"] = ""
            rec["token"] = ln.token
            if was_parked and engine == "codex":
                rec["codex_session"] = ln.session
        if was_parked:
            rec.pop("parked", None)
            rec.pop("park_sample", None)
            _clear_flags(rec)
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
        existing = doc.get("workers", {}).get(wid)
        if existing and existing.get("worker") == "retired":
            raise EngineError(f"worker {wid} is retired; register a new worker id.")
        _register_worker(doc, wid, engine, session, pane, worktree, note=note)
        _event(plan_dir, world.now(), "", "worker-add", wid=wid, engine=engine, session=session,
               pane=pane, worktree=worktree)
    proofs = ("session-coord's board" if session else "") + (" and " if session and pane else "") \
        + ("its pane" if pane else "")
    return f"worker {wid} ({engine}) registered; liveness from {proofs or 'reports only'}"


def worker_retire(plan: Plan, plan_dir: Path, world: World, wid: str) -> str:
    """Close an idle worker's pane and clean linked worktree, keeping its branch and record."""
    def run(argv: list[str]) -> tuple[int, str, str]:
        try:
            return world.run(argv)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise EngineError(f"could not run {' '.join(argv[:2])} while retiring {wid}: {e}") from None

    tree_removed = False
    window_closed = False
    with store.transaction(plan_dir) as doc:
        w = doc.get("workers", {}).get(wid)
        if w is None:
            raise EngineError(f"no worker {wid!r} in plan {plan.name!r}.")
        if w.get("worker") == "retired":
            raise EngineError(f"worker {wid} is already retired.")
        sts = graph.states(plan, doc)
        active = [i for i in w.get("items", []) if sts.get(i) in graph.ACTIVE + graph.PARKED]
        if active:
            raise EngineError(f"worker {wid} has active item(s) {', '.join(active)}; finish them first.")
        tree = w.get("worktree", "")
        repo = next((plan.items[i].repo for i in w.get("items", []) if i in plan.items), "")
        if tree and Path(tree).exists():
            if any(other != wid and Path(rec.get("worktree", "")).resolve() == Path(tree).resolve()
                   for other, rec in doc.get("workers", {}).items()
                   if rec.get("worktree") and rec.get("worker") != "retired"):
                raise EngineError(f"worker {wid}'s worktree is shared with another worker.")
            rc, out, err = run(["git", "-C", tree, "worktree", "list", "--porcelain"])
            if rc != 0 or f"worktree {Path(tree).resolve()}\n" not in out:
                raise EngineError(f"worker {wid}'s tree is not a linked git worktree: {err.strip()}")
            main = out.splitlines()[0].removeprefix("worktree ")
            if Path(tree).resolve() == Path(main).resolve() or (repo and Path(tree).resolve() == Path(repo).resolve()):
                raise EngineError(f"worker {wid}'s worktree is the main checkout; cannot remove it.")
            rc, out, err = run(["git", "-C", tree, "status", "--porcelain", "--untracked-files=all"])
            if rc != 0:
                raise EngineError(f"cannot inspect worker {wid}'s tree: {err.strip()}")
            if out.strip():
                raise EngineError(f"worker {wid}'s worktree is dirty; commit or clean it before retiring.")
        pane = w.get("pane", "")
        if pane:
            identity = (w.get("token") if w.get("ours") else "") or w.get("session", "")
            owned = world.pane_has_session(pane, identity) if identity else None
            if owned is True:
                rc, out, err = run(["tmux", "kill-window", "-t", pane])
                if rc != 0:
                    raise EngineError(f"could not close worker {wid}'s window: {(err or out).strip()}")
                window_closed = True
            elif world.pane_exists(pane) is not False:
                raise EngineError(f"cannot prove pane {pane} still belongs to worker {wid}; "
                                  "close it manually before retiring.")
        if tree and Path(tree).exists():
            rc, out, err = run(["git", "-C", main, "worktree", "remove", tree])
            if rc != 0:
                raise EngineError(f"could not remove worker {wid}'s worktree: {(err or out).strip()}")
            tree_removed = True
        w["worker"] = "retired"
        w["retired_at"] = _iso(world.now())
        _event(plan_dir, world.now(), "", "worker-retire", wid=wid, pane=pane, worktree=tree)
    actions = (["window closed"] if window_closed else []) + (["clean worktree removed"] if tree_removed else [])
    return f"worker {wid} retired; {', '.join(actions) if actions else 'registry closed'}; branch kept"


def assign(plan: Plan, plan_dir: Path, world: World, iid: str, wid: str, note: str = "",
           *, adopted: bool = False) -> str:
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
        status = _idle_worker_status(plan, w, world, now)
        if w.get("worker") == "parked":
            raise EngineError(f"worker {wid} is parked; resume its item before assigning work.")
        if status in ("dead", "retired") or (status == "unknown" and
                                             (w.get("worker") == "dead" or
                                              (w.get("items") and (w.get("pane") or w.get("session"))))):
            raise EngineError(f"worker {wid} is {status}; register or launch a live worker instead.")
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
                    "wid": wid, "engine": w.get("engine", "claude"),
                    "session": w.get("session", ""), "pane": w.get("pane", ""),
                    "worktree": w.get("worktree") or f"{it.repo.rstrip('/')}/.worktrees/{it.topic}",
                    "launched": _iso(now), "last_report": _iso(now), "worker": "starting",
                    "last_line": note or f"assigned to {wid}"})
        if w.get("ours"):
            rec["ours"] = True
        rec.pop("reopened", None)
        if iid not in w["items"]:
            w["items"].append(iid)
        _event(plan_dir, now, iid, "assign", wid=wid, note=note)
        if adopted:
            _event(plan_dir, now, iid, "adopt", wid=wid, session=w.get("session", ""),
                   pane=w.get("pane", ""))
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
    out = assign(plan, plan_dir, world, iid, wid, note or "adopted", adopted=True)
    return out.replace(f"{iid} assigned to", f"{iid} adopted: worker", 1) + \
        (f", session {session}" if session else "") + (f", pane {pane}" if pane else "")


# --- report, mark, attach -----------------------------------------------------------------

def _apply_report(plan: Plan, plan_dir: Path, doc: dict[str, Any], iid: str, state: str,
                  line: str, now: dt.datetime, verdict: oracles.Verdict | None,
                  evidence: str = "", pane: str = "") -> str:
    """Apply a command or file report while the plan transaction is held."""
    it = plan.items[iid]
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
           pane=pane, from_pm=bool(pane and doc.get("pm", {}).get("pane") == pane),
           **({"evidence": evidence} if evidence else {}))
    return outcome


def _message_pm(pm: dict[str, Any], world: World, plan_name: str,
                outcome: str, line: str) -> None:
    from_pm = bool(pm.get("pane")) and pm.get("pane") == world.pane()
    if pm.get("repo") and pm.get("target") and not from_pm:
        world.coord.msg(pm["repo"], pm["target"],
                        f"zamu: {outcome}. Worker says: {line}. Run `zamu pulse --plan "
                        f"{plan_name}`.")


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
        outcome = _apply_report(plan, plan_dir, doc, iid, state, line, now, verdict, evidence,
                                world.pane())
        pm = dict(doc.get("pm", {}))
    if state in ("review", "done") + FLAGS:
        _message_pm(pm, world, plan.name, outcome, line)
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
        old = doc.get("pm", {})
        doc["pm"] = {"repo": repo, "target": target, "pane": pane,
                     "tmux_session": tmux_session, "since": _iso(world.now()),
                     "status": "attached", "ask_count": old.get("ask_count", 0),
                     "dead_ask": old.get("dead_ask", "")}
    return f"worker reports will message {target} in {repo}"


def prepare_pm(plan: Plan, plan_dir: Path, cfg: Config, world: World, action: str,
               tmux_session: str = "") -> launch_mod.Launch:
    """The command for a fresh PM, after checking the previous target when there is one."""
    brief = plan_dir / "pm.md"
    if not brief.is_file():
        raise EngineError(f"{brief} is missing; write the plan's standing PM brief first.")
    pm = store.read(plan_dir).get("pm", {})
    repo = pm.get("repo") or world.cwd()
    session = tmux_session or pm.get("tmux_session", "")
    if not repo:
        raise EngineError("no PM repo is recorded and the current directory is unavailable.")
    if not session:
        raise EngineError("no PM tmux session is recorded; give --tmux-session.")
    if pm.get("status") == "starting":
        raise EngineError("a PM was just started; wait for it to attach before starting another.")
    target = pm.get("target", "")
    if target:
        live = world.coord.live_target(repo, target)
        if live is None:
            raise EngineError(f"could not check whether PM {target} is alive; retry when session-coord answers.")
        if live:
            raise EngineError(f"PM {target} is still alive; close it before starting another.")
    elif action == "restart":
        raise EngineError("no PM has been attached to restart; use `zamu pm start`.")
    return replace(launch_mod.plan_pm_launch(plan, repo, session, cfg.engines["claude"]),
                   pm_guard=(target, pm.get("status", ""), pm.get("since", "")))


def commit_pm(plan: Plan, plan_dir: Path, cfg: Config, world: World,
              ln: launch_mod.Launch, action: str) -> dict[str, str]:
    """Start the PM and record the fresh target under the plan lock."""
    # Recheck just before the state transaction, including session-coord's liveness answer.
    current = prepare_pm(plan, plan_dir, cfg, world, action, ln.tmux_session)
    if current.worktree != ln.worktree or current.pm_guard != ln.pm_guard:
        raise EngineError("the PM attachment changed since planning the launch; run the command again.")
    with store.transaction(plan_dir) as doc:
        pm = doc.get("pm", {})
        if (pm.get("target", ""), pm.get("status", ""), pm.get("since", "")) != ln.pm_guard:
            raise EngineError("the PM attachment changed while launching; run the command again.")
        got = launch_mod.execute(ln, world.run)
        doc["pm"] = {**pm, "repo": ln.worktree, "target": ln.pm_target, "pane": got.get("pane", ""),
                     "tmux_session": ln.tmux_session,
                     "status": "starting", "since": _iso(world.now())}
    return got


def check_pm(plan: Plan, plan_dir: Path, cfg: Config, world: World) -> list[str]:
    """Watch the attached PM and file one recovery ask for each confirmed outage."""
    snap = store.read(plan_dir)
    recorded = snap.get("pm", {})
    repo, target = recorded.get("repo", ""), recorded.get("target", "")
    if not repo or not target:
        return []
    alive = world.coord.live_target(repo, target)
    if alive is None:
        return []  # not seeing the board is not proof of death
    now = world.now()
    changes = []
    with store.transaction(plan_dir) as doc:
        pm = doc.get("pm", {})
        if pm.get("repo") != repo or pm.get("target") != target:
            return []  # an attach or launch changed the PM while we looked
        if alive:
            if pm.get("status") != "live":
                changes.append("PM seen alive")
            pm["status"] = "live"
            pm.pop("dead_ask", None)
            pm.pop("ask_error", None)
            return changes
        since = _parse(pm.get("since", ""))
        if (pm.get("status") == "starting" and since and
                now - since < dt.timedelta(minutes=cfg.launch_grace_minutes)):
            return []
        if pm.get("status") != "dead":
            changes.append("PM gone")
        pm["status"] = "dead"
        if pm.get("dead_ask") or cfg.escalate != "session-coord":
            return changes
        from . import render
        sts = graph.states(plan, doc)
        active = any(state in graph.ACTIVE for state in sts.values())
        attention = [line for line in render.summary(plan, doc, cfg, now)["attention"]
                     if not line.startswith(("PM gone", "PM recovery escalation"))]
        if not active and not attention:
            return changes
        count = pm.get("ask_count", 0) + 1
        tag = ask_tag(plan, f"pm-gone-{count}", "A")
        prompt = f"The PM for plan {plan.name} is gone; run `zamu pm restart {plan.name}`."
        ok, note = world.coord.ask_add(repo, tag, "A", prompt)
        if ok:
            pm["ask_count"] = count
            pm["dead_ask"] = tag
            pm.pop("ask_error", None)
            changes.append(f"PM recovery escalated as {tag}")
            _event(plan_dir, now, "pm", "pm_gone", tag=tag, repo=repo, note=note)
        else:
            pm["ask_error"] = note
            changes.append(f"PM recovery escalation failed: {note}")
    return changes


def stalled(plan: Plan, doc: dict[str, Any], cfg: Config, now: dt.datetime) -> list[str]:
    sts = graph.states(plan, doc)
    out = []
    for iid in plan.items:
        rec = doc["items"].get(iid, {})
        last = _parse(rec.get("last_report", ""))
        if (sts[iid] == "running" and not rec.get("waiting") and last and
                now - last > dt.timedelta(hours=cfg.stall_hours)):
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
                              protected=None if roots is None else procs_mod.protected_pids(ps, roots),
                              identities=worker_identities(plan, plan_dir, doc, sts),
                              plan_name=plan.name)
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
