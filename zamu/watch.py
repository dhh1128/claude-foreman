"""`zamu watch`: the mechanical loop, run by a process, so no PM has to be prodded.

Launching is mechanical. Once the user has approved a plan's dry run, "a slot is free and
item X is ready, so start it" needs no judgement, and it must not wait for a model to wake up.
Waiting inside a process costs nothing; waiting inside a model turn costs tokens, which is what
made a polling PM inefficient (Daniel, 2026-10-12: "A foreman that I have to prod to do
something isn't very helpful").

So the watcher does the mechanical part and the PM does the judgement. Each tick, for every
plan (or one):

- `pulse`, with oracles and pins only every `oracle_interval`, since they can call `gh` or git;
- on a plan with `autolaunch = true`, fill free slots from the ready set;
- turn the board's attention lines into stable keys, and message the PM through session-coord
  only about what is new since the last message: launches, items that changed state, and new
  attention. Busy admission has an episode key: it repeats only after an admission succeeds
  or an hour has passed.

Run it as a systemd user service (`zamu service install`), one watcher for the whole box, or
from a PM session as a background task with `--exit-on-news`, in which case it prints the news
and exits, and the harness wakes the PM.

This reverses the original "no daemon" decision, recorded in DESIGN.md: the reason given then,
that a daemon needs keeping alive across reboots, is what systemd provides.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config, engine, render, store
from .launch import LaunchError
from .plan import PlanError, load

DIGITS = re.compile(r"\d+")
BUSY_LINE = "launches wait, the box is busy: "
BUSY_REPEAT = dt.timedelta(hours=1)


def key(line: str) -> str:
    """An attention line with its numbers blanked, so an ageing wait is one key, not many."""
    return DIGITS.sub("#", line)


@dataclass
class Tick:
    plan: str
    launched: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    attention: list[str] = field(default_factory=list)   # new since the last message
    errors: list[str] = field(default_factory=list)

    @property
    def news(self) -> bool:
        return bool(self.launched or self.changes or self.attention or self.errors)

    def digest(self) -> str:
        parts = []
        if self.launched:
            parts.append("launched " + ", ".join(self.launched))
        if self.changes:
            parts.append("; ".join(self.changes))
        if self.attention:
            parts.append("needs you: " + "; ".join(self.attention))
        if self.errors:
            parts.append("errors: " + "; ".join(self.errors))
        return (f"zamu watch, plan {self.plan}: " + ". ".join(parts)
                + f". Run `zamu board --plan {self.plan}`.")


def world_for(doc: dict[str, Any], cfg: config.Config,
              base: Callable[[], engine.World]) -> engine.World:
    """The watcher has no project and no tmux session of its own: it borrows the PM's."""
    w = base()
    pm = doc.get("pm", {})
    w.cwd = lambda: pm.get("repo", "")
    sess = cfg.tmux_session or pm.get("tmux_session", "")
    w.tmux_session = lambda: sess
    return w


def tick(plan_dir: Path, cfg: config.Config, base_world: Callable[[], engine.World],
         oracles_due: bool, notify: bool = True) -> Tick:
    try:
        plan = load(plan_dir / "plan.toml", name=plan_dir.name)
    except PlanError as e:
        t = Tick(plan_dir.name)
        t.errors.append(f"plan does not load: {e}")
        return t
    t = Tick(plan.name)
    w = world_for(store.read(plan_dir), cfg, base_world)
    t.changes += engine.pulse(plan, plan_dir, cfg, w, oracles_due=oracles_due)["changes"]

    if plan.autolaunch:
        for iid in engine.ready_to_fill(plan, plan_dir, cfg, w):
            try:
                (ln,) = engine.prepare(plan, plan_dir, cfg, w, [iid])
                got = engine.commit_launch(plan, plan_dir, cfg, w, ln)
                t.launched.append(f"{iid} (pane {got.get('pane', '?')})")
            except engine.BusyError:
                pass  # a later reading refused; next tick will reconsider admission
            except (engine.EngineError, LaunchError, OSError) as e:
                t.errors.append(f"could not launch {iid}: {e}")

    t.changes += engine.check_pm(plan, plan_dir, cfg, w)

    with store.transaction(plan_dir) as doc:
        now = w.now()
        lines = render.summary(plan, doc, cfg, now)["attention"]
        watch_state = doc.setdefault("watch", {})
        seen = set(watch_state.get("notified", []))
        current = {key(a): a for a in lines if not a.startswith(BUSY_LINE)}
        t.attention = [a for k, a in current.items() if k not in seen]
        watch_state["notified"] = sorted(current)
        if doc.get("admission", {}).get("ok") is True:
            watch_state.pop("busy_notified_at", None)
        busy = next((a for a in lines if a.startswith(BUSY_LINE)), None)
        last = engine._parse(watch_state.get("busy_notified_at", ""))
        if busy and (last is None or now - last >= BUSY_REPEAT):
            t.attention.append(busy)
            watch_state["busy_notified_at"] = engine._iso(now)
        doc["watch"]["last_tick"] = engine._iso(now)
        pm = dict(doc.get("pm", {}))
    if notify and t.news and pm.get("repo") and pm.get("target") and pm.get("status") != "dead":
        ok, why = w.coord.msg(pm["repo"], pm["target"], t.digest())
        if not ok:
            t.errors.append(f"could not message the PM: {why}")
    return t


def plan_dirs(ref: str | None) -> list[Path]:
    if ref:
        return [config.resolve_plan_dir(ref)]
    return [config.plans_dir() / n for n in config.known_plans()]


class AlreadyRunning(RuntimeError):
    pass


def _single_instance(name: str) -> int:
    """Hold an exclusive lock for this process's life; a second watcher refuses to start."""
    config.home().mkdir(parents=True, exist_ok=True)
    fd = os.open(config.home() / f".{name}.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise AlreadyRunning(f"another `zamu watch` holds {config.home() / f'.{name}.lock'}; "
                             "`zamu service status` shows whether it is the service.") from None
    return fd


def log(line: str) -> None:
    p = config.home() / "watch.log"
    try:
        if p.exists() and p.stat().st_size > 1_000_000:
            tail = p.read_text()[-200_000:]
            p.write_text(tail[tail.find("\n") + 1:])
        with open(p, "a") as f:
            f.write(f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def run(ref: str | None, interval: float, oracle_interval: float, once: bool,
        exit_on_news: bool, base_world: Callable[[], engine.World],
        out: Callable[[str], None] = print) -> int:
    lock = _single_instance("watch-" + (Path(ref).name if ref else "all"))
    stop = {"now": False}

    def _term(*_a):
        stop["now"] = True
    signal.signal(signal.SIGTERM, _term)
    last_oracles = 0.0
    log(f"start: plans={ref or 'all'} interval={interval}s oracles every {oracle_interval}s")
    try:
        while not stop["now"]:
            cfg = config.load()  # re-read: a raised cap or new engine takes effect next tick
            due = time.monotonic() - last_oracles >= oracle_interval
            if due:
                last_oracles = time.monotonic()
            news = []
            for d in plan_dirs(ref):
                try:
                    t = tick(d, cfg, base_world, due, notify=not exit_on_news)
                except (store.StoreError, TimeoutError, OSError) as e:
                    t = Tick(d.name, errors=[str(e)])
                if t.news:
                    log(t.digest())
                    news.append(t.digest())
            if news and (exit_on_news or once):
                out("\n".join(news))
                return 0
            if once:
                return 0
            for _ in range(int(interval * 10)):
                if stop["now"]:
                    break
                time.sleep(0.1)
    finally:
        log("stop")
        os.close(lock)
    return 0


# --- the service ---------------------------------------------------------------------------

UNIT = "zamu-watch.service"

SHIM = """#!/bin/sh
# Stable entry point for the zamu plugin, written by `zamu service install`. The plugin's own
# path changes on every update; this follows Claude Code's install record instead.
d=$(python3 -c 'import json,os,sys
p=os.path.expanduser("~/.claude/plugins/installed_plugins.json")
try: print(json.load(open(p))["plugins"]["zamu@zamu"][0]["installPath"])
except Exception: sys.exit(1)') || d={fallback}
exec "$d/bin/zamu" "$@"
"""


def service_files(plugin_root: Path, env: dict[str, str]) -> tuple[Path, str, Path, str]:
    shim = Path("~/.local/share/zamu/bin/zamu").expanduser()
    unit = Path("~/.config/systemd/user").expanduser() / UNIT
    envs = "".join(f'Environment="{k}={v}"\n' for k, v in sorted(env.items()))
    body = f"""[Unit]
Description=zamu watch: launch ready work into free slots, and wake each plan's PM when it needs judgement
After=default.target

[Service]
ExecStart={shim} watch
Restart=on-failure
RestartSec=30
Nice=10
{envs}
[Install]
WantedBy=default.target
"""
    return shim, SHIM.format(fallback=str(plugin_root)), unit, body


SERVICE_ENV = ("PATH", "ZAMU_HOME", "ZAMU_CONFIG", "ZAMU_USER", "SESSION_COORD_USER",
               "FOREMAN_HOME", "FOREMAN_CONFIG", "FOREMAN_USER", "TMUX_TMPDIR")
