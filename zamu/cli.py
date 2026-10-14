"""The `zamu` command. Argument parsing and printing only; the logic is in `engine`."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import config, engine, render, store
from .config import ConfigError
from .launch import LaunchError
from .oracles import OracleError
from .plan import PlanError, load

TEMPLATE = '''# zamu plan. Edit freely; `zamu check` validates, `zamu board` shows state.
# Items: kind = "build" (a worker does it), "decision" (the user decides), "external" (waits
# on the world). Oracles: "pr:owner/repo#N", "cmd:<shell>", "file:<path>", "manual".

[plan]
name = "{name}"
# due = 2026-11-17            # inherited by items without their own due date
# common_brief = "briefs/common.md"
# max_workers = 2             # can only lower the machine's cap

# [items.example]
# title = "What this item delivers"
# kind = "build"
# repo = "/abs/path/to/repo"
# topic = "example"           # worktree and branch name; default the id
# depends_on = []
# oracle = "pr:owner/repo#1"
# brief = "briefs/example.md"
# estimate = "3d"
'''

PM_TEMPLATE = '''# PM brief: plan {name}

You are the project manager for this plan. This file is your standing brief; update it when the plan's role, peers, or rules change so a fresh session can take over.

## First

1. Load the `zamu` skill and run `zamu addendum`. Read `skills/zamu/DESIGN.md` in the zamu repository for the decisions behind the tool.
2. Run `zamu attach --plan {name} --repo <your project repo> --target <this session's id or name>`.
3. Run `zamu pulse --plan {name}` and act on its attention lines.

## Plan {name}

Describe the plan's purpose, current priorities, critical path, and decisions already made. Keep facts needed by the next PM on disk in the plan and its briefs.

## Peers and standing rules

List the sessions and repositories this PM coordinates with. Record rules for review, merges, approvals, and outside communication here or with `zamu rules`.

## Operations

Watch worker reports, check exit conditions, relay review findings, and keep the plan's owned documents current. Use `zamu board --plan {name}` for the compact state. The watcher launches approved work and calls attention to changes; the PM handles judgement.
'''


def _tmux_session() -> str:
    if not os.environ.get("TMUX"):
        return ""
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "#S"], capture_output=True,
                           text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _pane_exists(pane: str) -> bool | None:
    """Whether tmux lists this pane, or None when tmux could not be asked."""
    try:
        panes = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return pane in panes.stdout.splitlines() if panes.returncode == 0 else None


def _pane_has_session(pane: str, sid: str) -> bool | None:
    """Does a process in this pane carry `sid`? A present pane without an id proves no owner."""
    exists = _pane_exists(pane)
    if exists is not True:
        return exists
    if not sid:
        return None
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#{pane_pid}"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        root = int(r.stdout.strip())
        ps = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True,
                            timeout=5)
        if ps.returncode != 0:
            return None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    kids: dict[int, list[tuple[int, str]]] = {}
    for line in ps.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append((int(parts[0]), parts[2] if len(parts) > 2 else ""))
    args_of = {pid: a for v in kids.values() for pid, a in v}
    todo, seen = [root], set()
    while todo:
        pid = todo.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if sid in args_of.get(pid, ""):
            return True
        todo.extend(c for c, _ in kids.get(pid, []))
    return False


def _pane_activity(pane: str) -> float | None:
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#{window_activity}"],
                           capture_output=True, text=True, timeout=5)
        return float(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _pane_pids() -> list[int] | None:
    try:
        r = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_pid}"], capture_output=True,
                           text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        # No server at all means no panes; any other failure means we could not look.
        return [] if "no server running" in r.stderr else None
    try:
        return [int(x) for x in r.stdout.split()]
    except ValueError:
        return None


def _pane_pid(pane: str) -> int | None:
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#{pane_pid}"],
                           capture_output=True, text=True, timeout=5)
        return int(r.stdout.strip()) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _codex_session(tree: str, since: str) -> str | None:
    """Find one Codex rollout started in this worktree after zamu launched it."""
    import datetime as dt
    try:
        start = dt.datetime.fromisoformat(since)
    except ValueError:
        return None
    root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
    matches = set()
    try:
        for path in root.rglob("*.jsonl"):
            if dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc) < start:
                continue
            with path.open() as f:
                row = json.loads(f.readline())
            if row.get("type") != "session_meta":
                continue
            meta = row.get("payload", {})
            created = dt.datetime.fromisoformat(meta.get("timestamp", ""))
            if meta.get("cwd") == tree and created >= start:
                matches.add(meta.get("id") or meta.get("session_id"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    matches.discard(None)
    return next(iter(matches)) if len(matches) == 1 else None


def world() -> engine.World:
    from . import budgets, procs
    return engine.World(tmux_session=_tmux_session, pane_exists=_pane_exists,
                        pane_has_session=_pane_has_session,
                        scan=procs.scan, pane_activity=_pane_activity, pane_pids=_pane_pids,
                        pane_pid=_pane_pid,
                        codex_session=_codex_session,
                        budget_run=budgets.run)


def _load(args):
    d = config.resolve_plan_dir(args.plan)
    return load(d / "plan.toml", name=d.name), d


def cmd_init(args) -> int:
    target = config.plans_dir() / args.name
    if target.exists() or target.is_symlink():
        raise ConfigError(f"a plan named {args.name!r} already exists at {target}.")
    d = Path(args.dir).expanduser().resolve() if args.dir else target
    d.mkdir(parents=True, exist_ok=True)
    (d / "briefs").mkdir(exist_ok=True)
    if not (d / "plan.toml").exists():
        (d / "plan.toml").write_text(TEMPLATE.format(name=args.name))
    if not (d / "pm.md").exists():
        (d / "pm.md").write_text(PM_TEMPLATE.format(name=args.name))
    if args.dir:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(d, target_is_directory=True)
    print(f"plan {args.name}: {d / 'plan.toml'}")
    return 0


def cmd_plans(args) -> int:
    for n in config.known_plans():
        print(f"{n}  {(config.plans_dir() / n).resolve()}")
    return 0


def cmd_check(args) -> int:
    plan, d = _load(args)
    print(f"plan {plan.name} OK: {len(plan.items)} items, {d / 'plan.toml'}")
    return 0


def cmd_board(args) -> int:
    plan, d = _load(args)
    cfg = config.load()
    engine.refresh_idle_workers(plan, d, world())
    doc = store.read(d)
    now = engine.utcnow()
    if args.json:
        print(json.dumps(render.summary(plan, doc, cfg, now), indent=1, default=str))
    else:
        sys.stdout.write(render.board(plan, doc, cfg, now))
    return 0


def cmd_portfolio(args) -> int:
    from . import portfolio, procs
    w = world()
    w.scan = procs.sample_cpu
    data = portfolio.snapshot(config.load(), w)
    print(json.dumps(data, indent=1, default=str) if args.json else portfolio.board(data), end="" if not args.json else "\n")
    return 0


def cmd_pulse(args) -> int:
    plan, d = _load(args)
    cfg = config.load()
    w = world()
    res = engine.pulse(plan, d, cfg, w)
    for c in res["changes"]:
        print(f"* {c}")
    sys.stdout.write(render.board(plan, store.read(d), cfg, w.now()))
    return 0


def cmd_launch(args) -> int:
    plan, d = _load(args)
    cfg = config.load()
    w = world()
    ids = list(args.items)
    if args.ready:
        ids += [i for i in engine.ready_to_fill(plan, d, cfg, w) if i not in ids]
    if not ids:
        doc = store.read(d)
        summary = render.summary(plan, doc, cfg, w.now())
        reasons = [x for x in summary['waiting'] if 'ready, waits' in x]
        caps = [x for x in summary['attention'] if ' cap ' in x and 'held by' in x]
        print("nothing to launch: " + ("; ".join(reasons + caps) if reasons or caps else
                                     "no ready build items, or no free slots."))
        return 0
    launches = engine.prepare(plan, d, cfg, w, ids, relaunch=args.relaunch)
    for ln in launches:
        print(f"# {ln.item}: worktree {ln.worktree}, session {ln.session}"
              + (" (resume)" if ln.resume else "")
              + f", tier {ln.tier or 'default'}, model {ln.model or 'default'}"
              + (f", reasoning {ln.reasoning_effort}" if ln.reasoning_effort else ""))
        for s in ln.steps:
            print(s.shell())
        if args.go:
            got = engine.commit_launch(plan, d, cfg, w, ln)
            print(f"# launched {ln.item} in pane {got.get('pane', '?')}")
    if not args.go:
        print("# dry run: nothing was executed. Add --go to run these. (The session id is "
              "fresh on each launch.)")
    return 0


def cmd_resume(args) -> int:
    plan, d = _load(args)
    cfg, w = config.load(), world()
    ln, = engine.prepare(plan, d, cfg, w, [args.item], parked=True,
                         unblocked=args.unblocked or "")
    got = engine.commit_launch(plan, d, cfg, w, ln)
    print(f"resumed {args.item} in pane {got.get('pane', '?')}")
    return 0


def cmd_adopt(args) -> int:
    plan, d = _load(args)
    wt = str(Path(args.worktree).expanduser().resolve()) if args.worktree else ""
    print(engine.adopt(plan, d, world(), args.item, args.session or "", args.pane or "", wt,
                       args.note or "", engine=args.engine, wid=args.worker or ""))
    return 0


def cmd_worker(args) -> int:
    plan, d = _load(args)
    if args.action == "add":
        if not args.wid:
            raise ConfigError("`worker add` needs an id.")
        wt = str(Path(args.worktree).expanduser().resolve()) if args.worktree else ""
        print(engine.worker_add(d, world(), args.wid, args.engine, args.session or "",
                                args.pane or "", wt, args.note or ""))
        return 0
    if args.action == "retire":
        if not args.wid:
            raise ConfigError("`worker retire` needs an id.")
        print(engine.worker_retire(plan, d, world(), args.wid))
        return 0
    engine.refresh_idle_workers(plan, d, world())
    doc = store.read(d)
    ws = doc.get("workers", {})
    if not ws:
        print("no workers registered.")
    for wid, w in ws.items():
        items = ", ".join(w.get("items", [])) or "-"
        where = " ".join(x for x in (w.get("pane", ""), w.get("worktree", "")) if x)
        print(f"{wid}  {w.get('engine', '?')}  {w.get('worker', '-')}  items: {items}  {where}")
    return 0


def cmd_assign(args) -> int:
    plan, d = _load(args)
    print(engine.assign(plan, d, world(), args.item, args.wid, args.note or ""))
    it = plan.items[args.item]
    rec = store.read(d)["items"][args.item]
    from .launch import worker_prompt
    print("# Deliver this brief to the worker yourself (session-coord msg for a Claude "
          "session, the pane for a Codex window, SendMessage for a subagent):")
    print(worker_prompt(plan, it, rec.get("worktree", ""), False))
    return 0


def cmd_verify(args) -> int:
    plan, d = _load(args)
    print(engine.verify(plan, d, world(), args.item, ok=args.ok or "", fail=args.fail or ""))
    return 0


def cmd_rules(args) -> int:
    plan, _d = _load(args)
    p = Path(plan.rules)
    if args.add:
        p.parent.mkdir(parents=True, exist_ok=True)
        src = f" ({args.source})" if args.source else ""
        with open(p, "a") as f:
            f.write(f"- {engine.utcnow().date()}{src}: {' '.join(args.add.split())}\n")
        print(f"rule recorded in {p}")
        return 0
    try:
        text = p.read_text()
    except FileNotFoundError:
        text = ""
    print(f"# standing rules for plan {plan.name} ({p})")
    print(text.rstrip() or "(none yet)")
    if plan.owns:
        print("# documents the PM maintains: " + ", ".join(plan.owns))
    return 0


def cmd_report(args) -> int:
    plan, d = _load(args)
    print(engine.report(plan, d, world(), args.item, args.state, args.line,
                        evidence=args.evidence or ""))
    return 0


def cmd_mark(args) -> int:
    plan, d = _load(args)
    print(engine.mark(plan, d, world(), args.item, args.state, args.note or "",
                      evidence=args.evidence or ""))
    return 0


def cmd_attach(args) -> int:
    _plan, d = _load(args)
    target = args.target or os.environ.get("TMUX_PANE", "")
    if not target:
        raise ConfigError("give --target: this session's name, pane or session id, as "
                          "session-coord msg accepts it.")
    repo = str(Path(args.repo or os.getcwd()).expanduser().resolve())
    print(engine.attach(d, world(), repo, target, os.environ.get("TMUX_PANE", ""),
                        _tmux_session()))
    return 0


def cmd_pm(args) -> int:
    d = config.resolve_plan_dir(args.name)
    plan = load(d / "plan.toml", name=d.name)
    cfg = config.load()
    w = world()
    ln = engine.prepare_pm(plan, d, cfg, w, args.action, args.tmux_session or "")
    for step in ln.steps:
        print(step.shell())
    if args.go:
        got = engine.commit_pm(plan, d, cfg, w, ln, args.action)
        print(f"# started PM for {plan.name} in pane {got.get('pane', '?')}")
    else:
        print("# dry run: nothing was executed. Add --go to start a fresh PM.")
    return 0


def cmd_show(args) -> int:
    plan, d = _load(args)
    if args.item not in plan.items:
        raise PlanError(f"no item {args.item!r}.")
    sys.stdout.write(render.show(plan, store.read(d), args.item, store.events(d, args.item),
                                 config.load()))
    return 0


def cmd_reap(args) -> int:
    plan, d = _load(args)
    lines = engine.reap(plan, d, config.load(), world(), args.go)
    print("\n".join(lines) if lines else "no leftover processes.")
    if lines and not args.go and any(l.startswith("would kill") for l in lines):
        print("# dry run: nothing was killed. Add --go to kill the ones marked 'would kill'.")
    return 0


def cmd_watch(args) -> int:
    from . import watch
    try:
        return watch.run(args.plan, args.interval, args.oracle_interval, args.once,
                         args.exit_on_news, world)
    except watch.AlreadyRunning as e:
        print(f"zamu: {e}", file=sys.stderr)
        return 75


def _systemctl(*a: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["systemctl", "--user", *a], capture_output=True, text=True, timeout=30)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def cmd_service(args) -> int:
    from . import watch
    root = Path(__file__).resolve().parents[1]
    env = {k: os.environ[k] for k in watch.SERVICE_ENV if os.environ.get(k)}
    shim, shim_body, unit, unit_body = watch.service_files(root, env)
    if args.action == "install":
        if args.dry_run:
            print(f"# {shim}\n{shim_body}\n# {unit}\n{unit_body}")
            return 0
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(shim_body)
        shim.chmod(0o755)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(unit_body)
        for step in (("daemon-reload",), ("enable", "--now", watch.UNIT)):
            rc, out = _systemctl(*step)
            if rc != 0:
                print(f"zamu: systemctl --user {' '.join(step)} failed: {out}\nWithout systemd, "
                      "run `zamu watch` yourself, or from a PM session as a background task with "
                      "--exit-on-news.", file=sys.stderr)
                return 2
        print(f"installed and started {watch.UNIT}; it runs {shim} watch")
        return 0
    if args.action == "uninstall":
        _systemctl("disable", "--now", watch.UNIT)
        for p in (unit, shim):
            if p.exists():
                p.unlink()
        _systemctl("daemon-reload")
        print(f"removed {watch.UNIT}")
        return 0
    rc, out = _systemctl("is-active", watch.UNIT)
    print(f"{watch.UNIT}: {out or 'unknown'}")
    logf = config.home() / "watch.log"
    if logf.exists():
        print("\n".join(logf.read_text().splitlines()[-5:]))
    return 0 if out == "active" else 3


def cmd_addendum(args) -> int:
    p = config.addendum_path()
    try:
        sys.stdout.write(p.read_text())
    except FileNotFoundError:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zamu", description="Hold a cross-repo plan, launch "
                                "worker sessions when dependencies clear, watch them, escalate.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("budget", help="probe Codex, OpenRouter, or Claude account headroom")

    def plan_arg(sp):
        sp.add_argument("--plan", help="plan name or path (default $ZAMU_PLAN, or the only plan)")

    s = sub.add_parser("init", help="create a plan")
    s.add_argument("name")
    s.add_argument("--dir", help="keep the plan here (e.g. in a private repo); linked by name")
    s.set_defaults(func=cmd_init)
    s = sub.add_parser("plans", help="list plans")
    s.set_defaults(func=cmd_plans)
    s = sub.add_parser("check", help="validate the plan file")
    plan_arg(s)
    s.set_defaults(func=cmd_check)
    s = sub.add_parser("board", help="the compact status a waking PM reads")
    plan_arg(s)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_board)
    s = sub.add_parser("portfolio", help="one compact board across all plans")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_portfolio)
    s = sub.add_parser("pulse", help="check oracles and workers, escalate, then print the board")
    plan_arg(s)
    s.set_defaults(func=cmd_pulse)
    s = sub.add_parser("launch", help="start workers (dry run unless --go)")
    plan_arg(s)
    s.add_argument("items", nargs="*")
    s.add_argument("--ready", action="store_true", help="fill free slots from the ready set")
    s.add_argument("--relaunch", action="store_true",
                   help="resume a dead worker's own conversation in a new window")
    s.add_argument("--go", action="store_true", help="execute; without it, only print")
    s.set_defaults(func=cmd_launch)
    s = sub.add_parser("resume", help="resume a parked worker in its saved conversation")
    plan_arg(s)
    s.add_argument("item")
    s.add_argument("--unblocked", metavar="WHY", help="PM says an unlabelled or unfiled wait is over")
    s.set_defaults(func=cmd_resume)
    s = sub.add_parser("adopt", help="take on a worker that was started by hand")
    plan_arg(s)
    s.add_argument("item")
    s.add_argument("--session", help="the worker's claude session id "
                   "(`session-coord board --repo <repo> --json` lists it); none for other engines")
    s.add_argument("--engine", default="claude", help="claude, codex, subagent, ...")
    s.add_argument("--worker", help="worker id (default: the item id)")
    s.add_argument("--pane", help="its tmux pane, e.g. %%140")
    s.add_argument("--worktree", help="where it works (default <repo>/.worktrees/<topic>)")
    s.add_argument("--note")
    s.set_defaults(func=cmd_adopt)
    s = sub.add_parser("report", help="a worker reports progress")
    plan_arg(s)
    s.add_argument("item")
    s.add_argument("state", choices=engine.REPORT_STATES)
    s.add_argument("line")
    s.add_argument("--evidence", help="where the proof lives: a path, a URL, a run id")
    s.set_defaults(func=cmd_report)
    s = sub.add_parser("mark", help="close a manual item, abandon one, or reset it")
    plan_arg(s)
    s.add_argument("item")
    s.add_argument("state", choices=engine.MARK_STATES)
    s.add_argument("--note")
    s.add_argument("--evidence", help="where the proof lives, when marking done")
    s.set_defaults(func=cmd_mark)
    s = sub.add_parser("attach", help="tell zamu where the PM session is, so reports wake it")
    plan_arg(s)
    s.add_argument("--repo", help="the PM session's project (default cwd)")
    s.add_argument("--target", help="the PM session's name, pane or id (default $TMUX_PANE)")
    s.set_defaults(func=cmd_attach)
    s = sub.add_parser("pm", help="start or restart a plan's project manager")
    pm = s.add_subparsers(dest="action", required=True)
    for action in ("start", "restart"):
        ps = pm.add_parser(action, help="open a fresh PM window (dry run unless --go)")
        ps.add_argument("name", help="plan name or path")
        ps.add_argument("--tmux-session", help="tmux session for the new PM window")
        ps.add_argument("--go", action="store_true", help="execute the printed command")
        ps.set_defaults(func=cmd_pm)
    s = sub.add_parser("show", help="one item in full, with its recent events")
    plan_arg(s)
    s.add_argument("item")
    s.set_defaults(func=cmd_show)
    s = sub.add_parser("worker", help="register an agent someone else started, or list workers")
    plan_arg(s)
    s.add_argument("action", choices=["add", "list", "retire"])
    s.add_argument("wid", nargs="?")
    s.add_argument("--engine", default="claude", help="claude, codex, subagent, ...")
    s.add_argument("--session", help="a claude session id, for liveness via session-coord")
    s.add_argument("--pane", help="its tmux pane, for liveness via tmux")
    s.add_argument("--worktree")
    s.add_argument("--note")
    s.set_defaults(func=cmd_worker)
    s = sub.add_parser("assign", help="give an item to a registered worker; prints its brief")
    plan_arg(s)
    s.add_argument("item")
    s.add_argument("wid")
    s.add_argument("--note")
    s.set_defaults(func=cmd_assign)
    s = sub.add_parser("verify", help="re-establish a done item's evidence (after a pin moved)")
    plan_arg(s)
    s.add_argument("item")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--ok", metavar="EVIDENCE", help="a check run elsewhere passed; where its proof is")
    g.add_argument("--fail", metavar="WHY", help="it no longer holds: reopen the item")
    s.set_defaults(func=cmd_verify)
    s = sub.add_parser("rules", help="the plan's standing rules, or --add one the user just set")
    plan_arg(s)
    s.add_argument("--add", metavar="TEXT")
    s.add_argument("--source", help="where the rule came from, e.g. the ask id it answered")
    s.set_defaults(func=cmd_rules)
    s = sub.add_parser("reap", help="list leftover processes; kill those no live worker owns "
                       "(dry run unless --go)")
    plan_arg(s)
    s.add_argument("--go", action="store_true")
    s.set_defaults(func=cmd_reap)
    s = sub.add_parser("watch", help="the mechanical loop: pulse, autolaunch, wake PMs only "
                       "with news (all plans unless --plan)")
    plan_arg(s)
    s.add_argument("--interval", type=float, default=30.0, help="seconds between ticks")
    s.add_argument("--oracle-interval", type=float, default=300.0,
                   help="seconds between oracle and pin checks, which may call gh or git")
    s.add_argument("--once", action="store_true", help="one tick, print any news, exit")
    s.add_argument("--exit-on-news", action="store_true",
                   help="print the news and exit instead of messaging the PM: for a PM running "
                        "this as a background task")
    s.set_defaults(func=cmd_watch)
    s = sub.add_parser("service", help="run `zamu watch` as a systemd user service")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.add_argument("--dry-run", action="store_true", help="print the files install would write")
    s.set_defaults(func=cmd_service)
    s = sub.add_parser("addendum", help="print this machine's local rules for the skill")
    s.set_defaults(func=cmd_addendum)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["budget"]:
        from .budget_probes import main as budget_main
        return budget_main(argv[1:])
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (PlanError, ConfigError, OracleError, engine.EngineError, LaunchError,
            store.StoreError) as e:
        print(f"zamu: {e}", file=sys.stderr)
        return 2
    except TimeoutError as e:
        print(f"zamu: {e}", file=sys.stderr)
        return 75
