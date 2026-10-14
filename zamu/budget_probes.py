"""Standalone budget probes. Exit 0 admits, 75 waits, 2 means could not look."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo


def result(data: dict | None, why: str = "") -> tuple[int, dict]:
    if data is None:
        return 2, {"figure": why or "could not look"}
    return (75 if data.pop("blocked", False) else 0), data


def _last_rate_limits(path: Path) -> dict | None:
    # Ported from ~/code/me/devenv/prmerge/hostile.py. The guard searches past fresh
    # rollouts without limits and honours rate_limit_reached_type even without windows.
    found = None
    try:
        with path.open(errors="replace") as fh:
            for line in fh:
                if '"rate_limits"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                payload = event.get("payload") if isinstance(event, dict) else None
                limits = payload.get("rate_limits") if isinstance(payload, dict) else None
                if isinstance(limits, dict):
                    found = limits
    except OSError:
        return None
    return found


def codex(sessions: Path, threshold: float, now: dt.datetime) -> tuple[int, dict]:
    if not math.isfinite(threshold):
        return result(None, "invalid Codex threshold")
    try:
        files = sorted((p for p in sessions.rglob("*.jsonl") if p.is_file()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    limits = next((v for p in files if (v := _last_rate_limits(p)) is not None), None)
    if limits is None:
        return result(None, "no Codex rollout with rate limits could be read")
    live, reset, windows = 0, 0, []
    for name in ("primary", "secondary"):
        win = limits.get(name)
        if not isinstance(win, dict):
            continue
        try:
            pct = float(win["used_percent"])
            if not math.isfinite(pct):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        epoch = win.get("resets_at")
        if isinstance(epoch, (int, float)) and epoch <= now.timestamp():
            reset += 1
            continue
        live += 1
        windows.append((pct, epoch, name))
    reached = limits.get("rate_limit_reached_type")
    pct, epoch, name = max(windows, default=(None, None, ""), key=lambda w: w[0] or 0)
    blocked = bool((reached and (live or not reset)) or any(w[0] >= threshold for w in windows))
    try:
        when = dt.datetime.fromtimestamp(float(epoch), dt.timezone.utc).isoformat() if epoch else None
    except (TypeError, ValueError, OverflowError, OSError):
        when = None
    figure = (f"{pct:g}% of {name} window" if pct is not None else "Codex limit reached" if reached else "no live window")
    if reached and pct is not None and (live or not reset):
        figure += f"; {reached} reached"
    if when:
        figure += f", resets {when}"
    return result({"blocked": blocked, "used_pct": pct, "resets_at": when, "figure": figure})


def openrouter(min_usd: float, *, key: str | None = None, get=None) -> tuple[int, dict]:
    if not math.isfinite(min_usd):
        return result(None, "invalid OpenRouter threshold")
    try:
        if key is None:
            p = subprocess.run(["llm", "keys", "get", "openrouter"], capture_output=True,
                               text=True, timeout=10)
            if p.returncode:
                return result(None, "OpenRouter key unavailable")
            key = p.stdout.strip()
        if not key:
            return result(None, "OpenRouter key unavailable")

        def fetch(path):
            if get is not None:
                return get(path, key)
            req = urllib.request.Request("https://openrouter.ai/api/v1" + path,
                                         headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.load(response)

        info = fetch("/key")["data"]
        if info.get("limit") is not None:
            remaining = float(info["limit_remaining"])
        else:
            credits = fetch("/credits")["data"]
            remaining = float(credits["total_credits"]) - float(credits["total_usage"])
        if not math.isfinite(remaining):
            return result(None, "OpenRouter balance unreadable")
        return result({"blocked": remaining < min_usd, "figure": f"${remaining:.2f} remaining",
                       "remaining_usd": remaining})
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError,
            subprocess.TimeoutExpired) as e:
        return result(None, f"OpenRouter reading failed: {e}")


def _usage(projects: Path, since: dt.datetime, factors: dict | None = None) -> float | None:
    try:
        files = [p for p in projects.rglob("*.jsonl") if p.is_file() and p.stat().st_mtime >= since.timestamp()]
    except OSError:
        return None
    if not projects.is_dir():
        return None
    seen = set()
    total = 0.0
    for path in files:
        try:
            with path.open(errors="replace") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                        at = dt.datetime.fromisoformat(row.get("timestamp", "").replace("Z", "+00:00"))
                        message = row.get("message", {})
                        mid = message.get("id")
                        usage = message.get("usage")
                        if at < since or not mid or mid in seen or not isinstance(usage, dict):
                            continue
                        model = message.get("model", "").lower()
                        tier = "opus" if "opus" in model else "haiku" if "haiku" in model else "sonnet"
                        factor = (factors or {}).get(tier, {"opus": 5, "sonnet": 3, "haiku": 1}[tier])
                        cache = usage.get("cache_creation", {}) or {}
                        five = (cache.get("ephemeral_5m_input_tokens", 0) if cache else
                                usage.get("cache_creation_input_tokens", 0))
                        hour = cache.get("ephemeral_1h_input_tokens", 0)
                        total += factor * (usage.get("input_tokens", 0)
                                  + 1.25 * five + 2 * hour
                                  + 0.1 * usage.get("cache_read_input_tokens", 0)
                                  + 5 * usage.get("output_tokens", 0))
                        seen.add(mid)
                    except (ValueError, TypeError, AttributeError):
                        continue
        except OSError:
            return None
    return total


def _claude_window(now: dt.datetime, reset: str) -> tuple[dt.datetime, dt.datetime | None]:
    since = now - dt.timedelta(days=7)
    next_reset = None
    if reset:
        weekday, hour, zone = reset.split()
        day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(weekday[:3].lower())
        h, m = map(int, hour.split(":"))
        offset = ZoneInfo(zone) if not zone.startswith(('+', '-')) else dt.timezone(
            dt.timedelta(seconds=(1 if zone[0] == '+' else -1) *
                         (int(zone[1:3]) * 3600 + int(zone[4:6]) * 60)))
        local = now.astimezone(offset)
        anchor = local.replace(hour=h, minute=m, second=0, microsecond=0)
        anchor -= dt.timedelta(days=(anchor.weekday() - day) % 7)
        if anchor > local:
            anchor -= dt.timedelta(days=7)
        since, next_reset = anchor.astimezone(dt.timezone.utc), (anchor + dt.timedelta(days=7)).astimezone(dt.timezone.utc)
    return since, next_reset


def claude(projects: Path, threshold: float, allowance: float | None, now: dt.datetime,
           reset: str = "", factors: dict | None = None) -> tuple[int, dict]:
    if not math.isfinite(threshold):
        return result(None, "invalid Claude threshold")
    if not isinstance(factors or {}, dict) or any(not isinstance(v, (int, float)) or
                                                  not math.isfinite(v) or v <= 0
                                                  for v in (factors or {}).values()):
        return result(None, "invalid Claude model factors")
    try:
        since, next_reset = _claude_window(now, reset)
    except (ValueError, IndexError, KeyError):
        return result(None, "invalid reset (use 'Thu 17:00 UTC')")
    amount = _usage(projects, since, factors)
    if (amount is None or not math.isfinite(amount) or
            not isinstance(allowance, (int, float)) or not math.isfinite(allowance)
            or allowance <= 0):
        return result(None, "Claude transcripts or calibrated allowance unavailable")
    pct = 100 * amount / allowance
    figure = f"~{pct:.0f}% of allowance"
    if next_reset:
        figure += f", resets {next_reset:%a %H:%M}"
    else:
        figure += " (rolling 7d)"
    return result({"blocked": pct >= threshold, "used_pct": round(pct, 1), "estimate": True,
                   "resets_at": next_reset.isoformat() if next_reset else None,
                   "figure": figure})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="budget")
    sub = parser.add_subparsers(dest="engine", required=True)
    cx = sub.add_parser("codex")
    cx.add_argument("--sessions", type=Path, default=Path("~/.codex/sessions"))
    cx.add_argument("--max-pct", type=float, default=60)
    op = sub.add_parser("openrouter")
    op.add_argument("--min-usd", type=float, default=5)
    cl = sub.add_parser("claude")
    cl.add_argument("--projects", type=Path)
    cl.add_argument("--max-pct", type=float, default=85)
    cl.add_argument("--allowance", type=float)
    cl.add_argument("--reset", default="")
    cl.add_argument("action", nargs="?", choices=("calibrate",))
    cl.add_argument("pct", nargs="?", type=float)
    args = parser.parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    if args.engine == "codex":
        rc, data = codex(args.sessions.expanduser(), args.max_pct, now)
    elif args.engine == "openrouter":
        rc, data = openrouter(args.min_usd)
    else:
        path = Path("~/.config/budget/claude.toml").expanduser()
        try:
            settings = tomllib.loads(path.read_text())
        except FileNotFoundError:
            settings = {}
        except (OSError, ValueError) as e:
            print(json.dumps({"figure": f"Claude budget config unavailable: {e}"}))
            return 2
        projects = (args.projects or Path(settings.get("projects", "~/.claude/projects"))).expanduser()
        allowance = args.allowance or settings.get("allowance")
        reset = args.reset or settings.get("reset", "")
        factors = settings.get("model_factors", {})
        if not isinstance(factors, dict) or any(k not in ('opus', 'sonnet', 'haiku') or
                                                 not isinstance(v, (int, float)) or
                                                 not math.isfinite(v) or v <= 0
                                                 for k, v in factors.items()):
            print(json.dumps({"figure": "invalid Claude model factors"}))
            return 2
        if args.action == "calibrate":
            try:
                since, _next_reset = _claude_window(now, reset)
                amount = _usage(projects, since, factors)
            except (ValueError, IndexError, KeyError):
                amount = None
            if amount is None or amount <= 0 or args.pct is None or args.pct <= 0:
                rc, data = result(None, "cannot calibrate without readable transcripts and positive percentage")
            else:
                allowance = amount * 100 / args.pct
                path.parent.mkdir(parents=True, exist_ok=True)
                from . import store
                lines = [f"allowance = {allowance}", f"projects = {json.dumps(str(projects))}"]
                if reset:
                    lines.append(f"reset = {json.dumps(reset)}")
                if factors:
                    lines += ["", "[model_factors]"]
                    lines += [f"{name} = {value}" for name, value in factors.items()]
                try:
                    with store.locked(path.parent):
                        store._write_atomic(path, "\n".join(lines) + "\n")
                    rc, data = claude(projects, args.max_pct, allowance, now, reset, factors)
                except (OSError, TimeoutError) as e:
                    rc, data = result(None, f"could not save Claude calibration: {e}")
        else:
            rc, data = claude(projects, args.max_pct, allowance, now, reset, factors)
    print(json.dumps(data))
    return rc


if __name__ == "__main__":
    sys.exit(main())
