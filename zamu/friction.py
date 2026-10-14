"""Weekly friction reconstructed from plan event logs."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from . import store

KEYS = ("adopted", "revived", "admission_holds", "admission_hours", "budget_holds",
        "budget_hours", "nagged_waits", "pm_reports", "verify_reopens")


def _at(raw: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None


def totals(plan_dirs: list, now: dt.datetime) -> dict:
    week = (now - dt.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    previous = week - dt.timedelta(days=7)
    end = week + dt.timedelta(days=7)
    buckets = {"this_week": defaultdict(float), "previous_week": defaultdict(float)}
    unknown = False
    for d in plan_dirs:
        try:
            pm = store.read(d).get("pm", {}).get("pane", "")
            events = store.events(d)
        except (store.StoreError, OSError):
            unknown = True
            continue
        dead = set()
        holds = {}
        for ev in events:
            at = _at(ev.get("t"))
            if at is None or at > now:
                continue
            bucket = "this_week" if week <= at < end else "previous_week" if previous <= at < week else None
            what, item = ev.get("what"), ev.get("item", "")
            if what == "worker":
                if ev.get("worker") == "dead":
                    dead.add(item)
                elif ev.get("worker") == "live" and item in dead:
                    if bucket:
                        buckets[bucket]["revived"] += 1
                    dead.discard(item)
            if what == "launch_hold":
                reason = ev.get("reason")
                if reason not in ("admission", "budget"):
                    continue
                key = (item, reason)
                if ev.get("state") == "start":
                    if key not in holds:
                        holds[key] = at
                        if bucket:
                            buckets[bucket][reason + "_holds"] += 1
                elif ev.get("state") == "end" and key in holds:
                    _hours(buckets, reason + "_hours", holds.pop(key), at, previous, week, end)
            if not bucket:
                continue
            if what == "adopt":
                buckets[bucket]["adopted"] += 1
            elif what == "wait_nag":
                buckets[bucket]["nagged_waits"] += 1
            elif what == "report" and pm and ev.get("pane") == pm:
                buckets[bucket]["pm_reports"] += 1
            elif what == "verify" and ev.get("ok") is False:
                buckets[bucket]["verify_reopens"] += 1
        for (_item, reason), start in holds.items():
            _hours(buckets, reason + "_hours", start, now, previous, week, end)
    return {**{key: {name: round(values[name], 1) if name.endswith("_hours") else int(values[name])
                    for name in KEYS} for key, values in buckets.items()}, "unknown": unknown}


def _hours(buckets, name, start, stop, previous, week, end):
    for label, lo, hi in (("previous_week", previous, week), ("this_week", week, min(end, stop))):
        seconds = (min(stop, hi) - max(start, lo)).total_seconds()
        if seconds > 0:
            buckets[label][name] += seconds / 3600
