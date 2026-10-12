"""What the plan and the record together say: each item's state, the ready set, what is late.

Pure. The plan says what depends on what; the state says what has happened; this combines
them. Nothing here touches git, tmux, gh or the clock -- `today` is passed in -- which is what
lets the acceptance scenarios be tested as literals.

States:
    blocked    a dependency is not done
    ready      every dependency is done; a build item can launch, a decision needs an answer,
               an external item is waiting on the world
    running    a worker was launched and has not reported a PR
    review     the worker reports a PR open, or claimed done and the oracle has not agreed
    done       the oracle held, or a human marked it, or the plan file pins it
    abandoned  the plan file or a human said so; dependents are stranded, not unblocked

Deadlines are a check, not a scheduler. Each item may carry `due` and `estimate`; an item is
*at risk* when the longest chain of unfinished estimates ending at it cannot finish by its due
date starting today. That answers "what is late" for a plan with a hard date without
pretending to know who works on what, or when.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from .plan import Item, Plan

ACTIVE = ("running", "review")
FINAL = ("done", "abandoned")


def states(plan: Plan, doc: Mapping[str, Any]) -> dict[str, str]:
    recs = doc.get("items", {})
    out: dict[str, str] = {}

    def st(iid: str) -> str:
        if iid in out:
            return out[iid]
        it = plan.items[iid]
        rec = recs.get(iid, {})
        if it.pinned:
            s = it.pinned
        elif rec.get("state") in FINAL + ACTIVE:
            s = rec["state"]
        elif all(st(d) == "done" for d in it.depends_on):
            s = "ready"
        else:
            s = "blocked"
        out[iid] = s
        return s

    for iid in plan.items:  # validated acyclic, so the recursion terminates
        st(iid)
    return out


def launchable(plan: Plan, sts: Mapping[str, str]) -> list[str]:
    """Build items whose dependencies are done and which have no worker yet, in plan order."""
    return [i for i, it in plan.items.items() if it.kind == "build" and sts[i] == "ready"]


def stranded(plan: Plan, sts: Mapping[str, str]) -> dict[str, list[str]]:
    """Unfinished items that wait, directly or not, on something abandoned."""
    out: dict[str, list[str]] = {}
    for iid, it in plan.items.items():
        if sts[iid] in FINAL:
            continue
        bad = [d for d in _ancestors(plan, iid) if sts[d] == "abandoned"]
        if bad:
            out[iid] = sorted(bad)
    return out


def _ancestors(plan: Plan, iid: str) -> set[str]:
    seen: set[str] = set()
    todo = list(plan.items[iid].depends_on)
    while todo:
        d = todo.pop()
        if d not in seen:
            seen.add(d)
            todo.extend(plan.items[d].depends_on)
    return seen


@dataclass(frozen=True)
class Risk:
    item: str
    due: dt.date
    needs_days: int       # days of unfinished estimate on the longest chain ending here
    over_days: int        # how far past `due` that chain finishes; >0 means at risk
    chain: tuple[str, ...] = field(default=())
    overdue: bool = False


def risks(plan: Plan, sts: Mapping[str, str], today: dt.date) -> list[Risk]:
    """Items whose due date has passed or whose dependency chain cannot finish in time."""
    memo: dict[str, tuple[float, tuple[str, ...]]] = {}

    def finish(iid: str) -> tuple[float, tuple[str, ...]]:
        if iid in memo:
            return memo[iid]
        it = plan.items[iid]
        if sts[iid] in FINAL:
            memo[iid] = (0.0, ())
            return memo[iid]
        best: tuple[float, tuple[str, ...]] = (0.0, ())
        for d in it.depends_on:
            f = finish(d)
            if f[0] > best[0]:
                best = f
        memo[iid] = (best[0] + it.estimate_days, best[1] + (iid,))
        return memo[iid]

    out = []
    for iid, it in plan.items.items():
        if it.due is None or sts[iid] in FINAL:
            continue
        days, chain = finish(iid)
        needs = math.ceil(days)
        over = (today + dt.timedelta(days=needs) - it.due).days
        overdue = today > it.due
        if over > 0 or overdue:
            out.append(Risk(iid, it.due, needs, max(over, 0), chain, overdue))
    out.sort(key=lambda r: (not r.overdue, -r.over_days, r.due, r.item))
    return out


def inverted_dues(plan: Plan) -> list[tuple[str, str]]:
    """(item, dependency) pairs where the dependency is due after the thing that needs it.

    Only dates written on the dependency count. A plan-wide due date inherited by every item
    would otherwise flag every item that has its own, earlier, date as inverted.
    """
    out = []
    for iid, it in plan.items.items():
        for d in it.depends_on:
            dd = plan.items[d].due
            if it.due and dd and plan.items[d].due_own and dd > it.due:
                out.append((iid, d))
    return out


def contention(plan: Plan, sts: Mapping[str, str]) -> dict[str, list[str]]:
    """Repos with two or more build items ready or in flight: their merges will queue."""
    by_repo: dict[str, list[str]] = {}
    for iid, it in plan.items.items():
        if it.kind == "build" and sts[iid] in ("ready",) + ACTIVE:
            by_repo.setdefault(it.repo.rstrip("/"), []).append(iid)
    return {r: ids for r, ids in by_repo.items() if len(ids) > 1}
