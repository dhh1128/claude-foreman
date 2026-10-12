"""The board: what a waking PM reads, in a few hundred tokens for a twenty-item plan.

The PM wakes every twenty minutes or so, and each wake costs context. So the board says what
needs attention first, gives in-flight work one line each, and compresses everything that is
merely waiting into a list of ids. It never includes a transcript, a log or a brief; a PM that
wants more asks for one item with `foreman show <id>`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import engine, graph
from .config import Config
from .plan import Plan


def _short(s: str, n: int = 70) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _ago(iso: str, now: dt.datetime) -> str:
    t = engine._parse(iso)
    if not t:
        return "?"
    m = int((now - t).total_seconds() // 60)
    return f"{m}m" if m < 120 else f"{m // 60}h" if m < 48 * 60 else f"{m // 1440}d"


def summary(plan: Plan, doc: dict[str, Any], cfg: Config, now: dt.datetime) -> dict[str, Any]:
    sts = graph.states(plan, doc)
    recs = doc.get("items", {})
    used, cap = engine.slots(plan, doc, cfg)
    attention: list[str] = []
    for iid in plan.items:
        r = recs.get(iid, {})
        if sts[iid] in graph.ACTIVE and r.get("worker") == "dead":
            attention.append(f"{iid}: worker gone, last \"{_short(r.get('last_line', ''), 50)}\" "
                             f"-> `foreman launch --relaunch {iid}` or `foreman mark {iid} reset`")
        if r.get("stuck") and sts[iid] not in graph.FINAL:
            attention.append(f"{iid}: stuck, \"{_short(r['stuck'])}\"")
        if r.get("waiting") and sts[iid] not in graph.FINAL:
            attention.append(f"{iid}: waiting on the user, \"{_short(r['waiting'])}\"")
        if r.get("claimed_done") and sts[iid] == "review":
            attention.append(f"{iid}: claims done; oracle says {_short(r.get('oracle', {}).get('detail', 'manual'), 60)}")
        if r.get("ask_error"):
            attention.append(f"{iid}: decision not escalated: {_short(r['ask_error'], 60)}")
    for iid in plan.items:
        r = recs.get(iid, {})
        quiet = r.get("pane_activity")
        if (sts[iid] == "running" and quiet and r.get("worker") != "dead"
                and not r.get("waiting") and not r.get("stuck")):
            mins = int((now.timestamp() - quiet) // 60)
            if mins >= cfg.pane_quiet_minutes:
                attention.append(f"{iid}: pane quiet {mins}m; it may be sitting at a prompt")
    by_item: dict[str, list[dict]] = {}
    for f in doc.get("leftovers", []):
        by_item.setdefault(f.get("item", ""), []).append(f)
    for item, fs in by_item.items():
        pids = " ".join(str(f["pid"]) for f in fs[:6]) + (" …" if len(fs) > 6 else "")
        why = sorted({w for f in fs for w in f["why"]})
        who = (f"{item}: tell its worker to kill them" if item and not all(f["reapable"] for f in fs)
               else f"{item or 'deleted worktrees'}: `foreman reap --go` kills them")
        attention.append(f"{len(fs)} leftover process{'es' if len(fs) > 1 else ''} "
                         f"({', '.join(why)}) pids {pids}; {who}")
    for iid in engine.stalled(plan, doc, cfg, now):
        attention.append(f"{iid}: no report for {_ago(recs[iid]['last_report'], now)}")
    for iid, bad in graph.stranded(plan, sts).items():
        attention.append(f"{iid}: stranded behind abandoned {', '.join(bad)}")
    for rk in graph.risks(plan, sts, now.date()):
        what = "overdue" if rk.overdue else f"{rk.over_days}d late"
        chain = "->".join(rk.chain) if len(rk.chain) > 1 else ""
        attention.append(f"{rk.item}: {what} (due {rk.due}, needs {rk.needs_days}d"
                         + (f" via {chain}" if chain else "") + ")")
    for item, dep in graph.inverted_dues(plan):
        attention.append(f"{item}: due before its dependency {dep}")
    for repo, ids in graph.contention(plan, sts).items():
        attention.append(f"merges queue in {repo}: {', '.join(ids)} "
                         "(serialize with `session-coord lock merge`)")

    active = []
    for iid in plan.items:
        if sts[iid] in graph.ACTIVE:
            r = recs.get(iid, {})
            active.append(f"{iid} {sts[iid]} {r.get('worker', '-')} "
                          f"{_ago(r.get('last_report', ''), now)}: \"{_short(r.get('last_line', ''), 60)}\"")
    launch = graph.launchable(plan, sts)
    waiting = []
    for iid, it in plan.items.items():
        if it.kind != "build" and sts[iid] == "ready":
            r = recs.get(iid, {})
            if it.kind == "decision":
                waiting.append(f"{iid} (decision{', asked ' + r['ask'] if r.get('ask') else ''})")
            else:
                ok = r.get("oracle", {}).get("ok")
                waiting.append(f"{iid} (external{', unknown' if ok is None and r.get('oracle') else ''})")
    blocked = [f"{iid}<-{','.join(d for d in plan.items[iid].depends_on if sts[d] != 'done')}"
               for iid in plan.items if sts[iid] == "blocked"]
    counts = {s: sum(1 for v in sts.values() if v == s)
              for s in ("done", "running", "review", "ready", "blocked", "abandoned")}
    return {"plan": plan.name, "date": now.date().isoformat(), "workers": [used, cap],
            "counts": counts, "attention": attention, "active": active,
            "launchable": launch, "slots": max(cap - used, 0), "waiting": waiting,
            "blocked": blocked, "done": [i for i in plan.items if sts[i] == "done"],
            "states": sts}


def board(plan: Plan, doc: dict[str, Any], cfg: Config, now: dt.datetime) -> str:
    s = summary(plan, doc, cfg, now)
    c = s["counts"]
    head = (f"plan {s['plan']} {s['date']}  workers {s['workers'][0]}/{s['workers'][1]}  "
            + " ".join(f"{k} {v}" for k, v in c.items() if v))
    out = [head]
    if s["attention"]:
        out.append("attention:")
        out += [f"  {a}" for a in s["attention"]]
    if s["active"]:
        out.append("active:")
        out += [f"  {a}" for a in s["active"]]
    if s["launchable"]:
        n = s["slots"]
        out.append(f"ready to launch ({n} slot{'s' if n != 1 else ''} free): "
                   + ", ".join(s["launchable"]))
    if s["waiting"]:
        out.append("waiting: " + ", ".join(s["waiting"]))
    if s["blocked"]:
        out.append("blocked: " + " ".join(s["blocked"]))
    if s["done"]:
        out.append("done: " + ", ".join(s["done"]))
    return "\n".join(out) + "\n"


def show(plan: Plan, doc: dict[str, Any], iid: str, events: list[dict[str, Any]]) -> str:
    it = plan.items[iid]
    r = doc.get("items", {}).get(iid, {})
    st = graph.states(plan, doc)[iid]
    lines = [f"{iid}: {it.title}", f"  kind {it.kind}, state {st}"
             + (f", ref {it.ref}" if it.ref else "")]
    if it.repo:
        lines.append(f"  repo {it.repo}  topic {it.topic}")
    if it.depends_on:
        lines.append(f"  depends on {', '.join(it.depends_on)}")
    if it.oracle:
        lines.append(f"  oracle {' AND '.join(it.oracle)}")
    if it.brief:
        lines.append(f"  brief {it.brief}")
    if it.question:
        lines.append(f"  question {it.question}")
    if it.due:
        lines.append(f"  due {it.due}" + (f", estimate {it.estimate_days:g}d" if it.estimate_days else ""))
    for k in ("session", "pane", "worktree", "worker", "launched", "last_report", "last_line",
              "stuck", "waiting", "ask", "note"):
        if r.get(k):
            lines.append(f"  {k} {r[k]}")
    if r.get("oracle"):
        lines.append(f"  oracle said {r['oracle'].get('ok')} at {r['oracle'].get('checked')}: "
                     f"{r['oracle'].get('detail')}")
    if events:
        lines.append("  recent events:")
        for e in events[-8:]:
            rest = {k: v for k, v in e.items() if k not in ("t", "item", "what")}
            lines.append(f"    {e['t']} {e['what']} {_short(str(rest), 90)}")
    return "\n".join(lines) + "\n"
