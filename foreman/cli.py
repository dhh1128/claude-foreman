"""The `foreman` command. Argument parsing and printing only; the logic is in `engine`."""

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

TEMPLATE = '''# foreman plan. Edit freely; `foreman check` validates, `foreman board` shows state.
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


def _tmux_session() -> str:
    if not os.environ.get("TMUX"):
        return ""
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "#S"], capture_output=True,
                           text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _pane_has_session(pane: str, sid: str) -> bool | None:
    """Does any process in `pane` carry `sid` in its argv? None when it cannot be told."""
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#{pane_pid}"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            # tmux answered: the pane is gone. A missing server is a failure to look.
            return False if "can't find" in r.stderr else None
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


def world() -> engine.World:
    return engine.World(tmux_session=_tmux_session, pane_has_session=_pane_has_session)


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
    doc = store.read(d)
    now = engine.utcnow()
    if args.json:
        print(json.dumps(render.summary(plan, doc, cfg, now), indent=1, default=str))
    else:
        sys.stdout.write(render.board(plan, doc, cfg, now))
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
        ids += [i for i in engine.ready_to_fill(plan, d, cfg) if i not in ids]
    if not ids:
        print("nothing to launch: no ready build items, or no free slots.")
        return 0
    launches = engine.prepare(plan, d, cfg, w, ids, relaunch=args.relaunch)
    for ln in launches:
        print(f"# {ln.item}: worktree {ln.worktree}, session {ln.session}"
              + (" (resume)" if ln.resume else ""))
        for s in ln.steps:
            print(s.shell())
        if args.go:
            got = engine.commit_launch(plan, d, cfg, w, ln)
            print(f"# launched {ln.item} in pane {got.get('pane', '?')}")
    if not args.go:
        print("# dry run: nothing was executed. Add --go to run these. (The session id is "
              "fresh on each launch.)")
    return 0


def cmd_adopt(args) -> int:
    plan, d = _load(args)
    wt = str(Path(args.worktree).expanduser().resolve()) if args.worktree else ""
    print(engine.adopt(plan, d, world(), args.item, args.session, args.pane or "", wt,
                       args.note or ""))
    return 0


def cmd_report(args) -> int:
    plan, d = _load(args)
    print(engine.report(plan, d, world(), args.item, args.state, args.line))
    return 0


def cmd_mark(args) -> int:
    plan, d = _load(args)
    print(engine.mark(plan, d, world(), args.item, args.state, args.note or ""))
    return 0


def cmd_attach(args) -> int:
    _plan, d = _load(args)
    target = args.target or os.environ.get("TMUX_PANE", "")
    if not target:
        raise ConfigError("give --target: this session's name, pane or session id, as "
                          "session-coord msg accepts it.")
    repo = str(Path(args.repo or os.getcwd()).expanduser().resolve())
    print(engine.attach(d, world(), repo, target, os.environ.get("TMUX_PANE", "")))
    return 0


def cmd_show(args) -> int:
    plan, d = _load(args)
    if args.item not in plan.items:
        raise PlanError(f"no item {args.item!r}.")
    sys.stdout.write(render.show(plan, store.read(d), args.item, store.events(d, args.item)))
    return 0


def cmd_addendum(args) -> int:
    p = config.addendum_path()
    try:
        sys.stdout.write(p.read_text())
    except FileNotFoundError:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="foreman", description="Hold a cross-repo plan, launch "
                                "worker sessions when dependencies clear, watch them, escalate.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def plan_arg(sp):
        sp.add_argument("--plan", help="plan name or path (default $FOREMAN_PLAN, or the only plan)")

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
    s = sub.add_parser("adopt", help="take on a worker that was started by hand")
    plan_arg(s)
    s.add_argument("item")
    s.add_argument("--session", required=True, help="the worker's claude session id "
                   "(`session-coord board --repo <repo> --json` lists it)")
    s.add_argument("--pane", help="its tmux pane, e.g. %%140")
    s.add_argument("--worktree", help="where it works (default <repo>/.worktrees/<topic>)")
    s.add_argument("--note")
    s.set_defaults(func=cmd_adopt)
    s = sub.add_parser("report", help="a worker reports progress")
    plan_arg(s)
    s.add_argument("item")
    s.add_argument("state", choices=engine.REPORT_STATES)
    s.add_argument("line")
    s.set_defaults(func=cmd_report)
    s = sub.add_parser("mark", help="close a manual item, abandon one, or reset it")
    plan_arg(s)
    s.add_argument("item")
    s.add_argument("state", choices=engine.MARK_STATES)
    s.add_argument("--note")
    s.set_defaults(func=cmd_mark)
    s = sub.add_parser("attach", help="tell foreman where the PM session is, so reports wake it")
    plan_arg(s)
    s.add_argument("--repo", help="the PM session's project (default cwd)")
    s.add_argument("--target", help="the PM session's name, pane or id (default $TMUX_PANE)")
    s.set_defaults(func=cmd_attach)
    s = sub.add_parser("show", help="one item in full, with its recent events")
    plan_arg(s)
    s.add_argument("item")
    s.set_defaults(func=cmd_show)
    s = sub.add_parser("addendum", help="print this machine's local rules for the skill")
    s.set_defaults(func=cmd_addendum)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (PlanError, ConfigError, OracleError, engine.EngineError, LaunchError,
            store.StoreError) as e:
        print(f"foreman: {e}", file=sys.stderr)
        return 2
    except TimeoutError as e:
        print(f"foreman: {e}", file=sys.stderr)
        return 75
