"""Machine-wide engine budget readings; the probe owns the threshold policy."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Callable

from . import config, store

FILE = "budgets.json"


def run(argv: list[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run([str(Path(argv[0]).expanduser()), *[str(Path(a).expanduser()) if a.startswith("~/") else a for a in argv[1:]]],
                           capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 2, "", str(e)


def probe(command: tuple[str, ...], runner: Callable[[list[str]], tuple[int, str, str]]) -> dict:
    try:
        rc, out, err = runner(list(command))
    except Exception as e:  # a probe failure is an unknown reading, never a zero reading
        return {"verdict": "unknown", "why": str(e), "data": {}}
    if not out.strip() and rc not in (0, 75):
        return {"verdict": "unknown", "why": (err or f"exit {rc}").strip(), "data": {}}
    try:
        data = json.loads(out)
        if not isinstance(data, dict):
            raise ValueError("stdout is not a JSON object")
    except (ValueError, TypeError) as e:
        return {"verdict": "unknown", "why": f"invalid probe JSON: {e}", "data": {}}
    if rc == 0:
        return {"verdict": "admit", "data": data}
    if rc == 75:
        return {"verdict": "not-now", "data": data}
    return {"verdict": "unknown", "why": (err or out or f"exit {rc}").strip(), "data": data}


def read() -> dict | None:
    p = config.home() / FILE
    try:
        doc = json.loads(p.read_text())
        if not isinstance(doc, dict) or any(not isinstance(v, dict) or
                                            not isinstance(v.get('data', {}), dict) or
                                            v.get('verdict') not in ('admit', 'not-now', 'unknown')
                                            for v in doc.values()):
            raise ValueError("expected an object")
        return doc
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None


def refresh(cfg, now: dt.datetime, runner, *, force: bool = False,
            names: tuple[str, ...] | None = None, locked: bool = False) -> dict:
    """Refresh under the machine lock; share identical argv and one reading across plans."""
    if not locked:
        with store.locked(config.home()):
            return refresh(cfg, now, runner, force=force, names=names, locked=True)
    doc = read()
    if doc is None:
        return {name: {"verdict": "unknown", "why": f"could not read {config.home() / FILE}",
                       "data": {}} for name in cfg.budgets}
    groups = {}
    for name in names if names is not None else cfg.budgets:
        command = cfg.budgets.get(name)
        if command:
            groups.setdefault(command, []).append(name)
    changed = False
    for command, group in groups.items():
        fresh = True
        for name in group:
            old = doc.get(name, {})
            try:
                at = dt.datetime.fromisoformat(old.get("at", ""))
                reset = old.get('data', {}).get('resets_at')
                reset_at = dt.datetime.fromisoformat(reset.replace('Z', '+00:00')) if reset else None
                fresh = fresh and now - at < dt.timedelta(minutes=cfg.budget_refresh_minutes)
                fresh = fresh and (reset_at is None or now < reset_at)
            except (ValueError, TypeError, AttributeError):
                fresh = False
        if not force and fresh:
            continue
        reading = {**probe(command, runner), "at": now.isoformat()}
        for name in cfg.budgets:
            if cfg.budgets[name] == command:
                doc[name] = reading
        changed = True
    if changed:
        store._write_atomic(config.home() / FILE, json.dumps(doc, sort_keys=True, indent=2) + "\n")
    return doc


def choice(names: tuple[str, ...], cfg, load: dict[str, int], readings: dict) -> tuple[str | None, str]:
    unknown = None
    causes = []
    for name in names:
        if name not in cfg.engines:
            causes.append(f"{name}: not configured")
            continue
        cap = cfg.engine_caps.get(name)
        if cap is not None and load.get(name, 0) >= cap:
            causes.append(f"{name}: cap {cap} reached")
            continue
        verdict = readings.get(name, {}).get("verdict", "unknown") if name in cfg.budgets else "admit"
        if verdict == "admit":
            return name, ""
        if verdict == "unknown" and unknown is None:
            unknown = name
        causes.append(f"{name}: {verdict}")
    return (unknown, "budget unknown") if unknown else (None, "; ".join(causes))
