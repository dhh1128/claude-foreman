"""The board: what a waking PM reads, in a few hundred tokens for a twenty-item plan.

The PM wakes every twenty minutes or so, and each wake costs context. So the board says what
needs attention first, gives in-flight work one line each, and compresses everything that is
merely waiting into a list of ids. It never includes a transcript, a log or a brief; a PM that
wants more asks for one item with `zamu show <id>`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import budgets, engine, graph
from .config import Config, ConfigError
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
    elsewhere = engine.busy_elsewhere(plan)
    attention: list[str] = []
    info: list[str] = []
    saved = budgets.read() if cfg.budgets else {}
    readings = (saved if saved is not None else
                {e: {"verdict": "unknown", "why": "could not read budgets.json", "data": {}}
                 for e in cfg.budgets})
    load = engine.engine_load(plan, doc) if cfg.engine_caps else {}
    pm = doc.get("pm", {})
    if pm.get("status") == "dead" and pm.get("dead_ask"):
        attention.append(f"PM gone; asked {pm['dead_ask']} to run `zamu pm restart {plan.name}`")
    if pm.get("ask_error"):
        attention.append(f"PM recovery escalation failed: {_short(pm['ask_error'], 70)}")
    for iid in plan.items:
        r = recs.get(iid, {})
        if sts[iid] in graph.ACTIVE and r.get("worker") == "dead":
            attention.append(f"{iid}: worker gone, last \"{_short(r.get('last_line', ''), 50)}\" "
                             f"-> `zamu launch --relaunch {iid}` or `zamu mark {iid} reset`")
        if r.get("stuck") and sts[iid] not in graph.FINAL + graph.PARKED:
            attention.append(f"{iid}: stuck, \"{_short(r['stuck'])}\"")
        target = r.get("wait_target", {})
        if r.get("waiting") and sts[iid] not in graph.FINAL + graph.PARKED:
            who = ("the PM" if target.get("kind") == "pm" else
                   f"ask {target['value']}" if target.get("kind") == "ask" else
                   target.get("value", "the user"))
            attention.append(f"{iid}: waiting on {who}, \"{_short(r['waiting'])}\"")
        if r.get("waiting") and target.get("changed") and sts[iid] not in graph.FINAL:
            if target["kind"] == "file":
                attention.append(f"{iid}: {target['value']} changed since it reported waiting on it; tell it to resume")
            elif target["kind"] == "ask":
                attention.append(f"{iid}: ask {target['value']} closed since it reported waiting on it; tell it to resume")
        if r.get("claimed_done") and sts[iid] == "review":
            attention.append(f"{iid}: claims done; oracle says {_short(r.get('oracle', {}).get('detail', 'manual'), 60)}")
        if r.get("ask_error"):
            attention.append(f"{iid}: decision not escalated: {_short(r['ask_error'], 60)}")
        for error in r.get("report_file_errors", []):
            attention.append(f"{iid}: {error}")
        if r.get("report_read_error") and sts[iid] in graph.ACTIVE:
            attention.append(f"{iid}: {r['report_read_error']}")
        if r.get("park_refusal") and sts[iid] in graph.ACTIVE:
            attention.append(f"{iid}: idle but cannot park: {_short(r['park_refusal'])}")
    for iid in plan.items:
        r = recs.get(iid, {})
        quiet = r.get("pane_activity")
        if (sts[iid] == "running" and quiet and r.get("worker") != "dead"
                and not r.get("waiting") and not r.get("stuck")):
            mins = int((now.timestamp() - quiet) // 60)
            if mins >= cfg.pane_quiet_minutes:
                attention.append(f"{iid}: pane quiet {mins}m; it may be sitting at a prompt")
    by_item: dict[tuple[str, bool], list[dict]] = {}
    for f in doc.get("leftovers", []):
        live_deleted = "live worker in a deleted worktree" in f["why"]
        by_item.setdefault((f.get("item", ""), live_deleted), []).append(f)
    for (item, live_deleted), fs in by_item.items():
        pids = " ".join(str(f["pid"]) for f in fs[:6]) + (" …" if len(fs) > 6 else "")
        why = sorted({w for f in fs for w in f["why"]})
        if live_deleted:
            info.append(f"{item}: worktree gone, worker still live")
            continue
        who = (f"{item}: tell its worker to kill them" if item and not all(f["reapable"] for f in fs)
               else f"{item or 'deleted worktrees'}: `zamu reap --go` kills them")
        attention.append(f"{len(fs)} leftover process{'es' if len(fs) > 1 else ''} "
                         f"({', '.join(why)}) pids {pids}; {who}")
    adm = doc.get("admission", {})
    if adm.get("ok") is False and engine.admission_can_block(plan, doc, cfg):
        attention.append(f"launches wait, the box is busy: {_short(adm.get('why', ''), 90)}")
    elif adm.get("ok") is None and adm.get("why"):
        attention.append(f"admission unknown, count cap only: {_short(adm['why'], 80)}")
    for name in cfg.budgets:
        reading = readings.get(name, {})
        if reading.get("verdict", "unknown") == "unknown":
            attention.append(f"{name} budget unknown: {_short(reading.get('why', 'could not look'), 80)}")
    episode = doc.get('budget_episode', {})
    budget_stalled = bool(cfg.budgets and engine.budget_waiting(plan, doc, cfg, readings))
    if episode.get('ask') == 'board' and budget_stalled:
        attention.append(_short(episode.get('line', 'budget trade-off needed'), 300))
    if episode.get('error') and budget_stalled:
        attention.append(f"budget escalation failed: {_short(episode['error'], 80)}")
    for iid in engine.stalled(plan, doc, cfg, now):
        attention.append(f"{iid}: no report for {_ago(recs[iid]['last_report'], now)}")
    for iid in plan.items:
        r = recs.get(iid, {})
        if r.get("stale") and sts[iid] == "done":
            attention.append(f"{iid}: evidence stale, {r['stale']} -> `zamu verify {iid}`")
        if r.get("reopened") and sts[iid] not in graph.FINAL:
            attention.append(f"{iid}: reopened, {_short(r['reopened'], 60)}")
    for iid, it in plan.items.items():
        days = engine.waited_days(doc, iid, now)
        if it.nag_after_days and days is not None and days > it.nag_after_days \
                and sts[iid] not in graph.FINAL:
            who = f" on {it.waiting_on}" if it.waiting_on else ""
            attention.append(f"{iid}: waiting{who} {int(days)}d (nag after "
                             f"{it.nag_after_days:g}d)")
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
    parked = []
    for iid in plan.items:
        if sts[iid] == "parked":
            r = recs.get(iid, {})
            info = r.get("parked", {})
            parked.append(f"{iid} (parked, waits on {_short(info.get('reason', '?'), 70)}"
                          f"{', asks ' + ', '.join(info['asks']) if info.get('asks') else ''})")
        if sts[iid] in graph.ACTIVE:
            r = recs.get(iid, {})
            who = f"[{r['wid']}] " if r.get("wid") and r["wid"] != iid else ""
            active.append(f"{iid} {who}{sts[iid]} {r.get('worker', '-')} "
                          f"{_ago(r.get('last_report', ''), now)}: \"{_short(r.get('last_line', ''), 60)}\"")
    launch = graph.launchable(plan, sts)
    waiting = []
    eligible = []
    for iid in launch:
        it = plan.items[iid]
        selected, reason = engine._choose(it, cfg, load, readings)
        if selected:
            try:
                cfg.model_for(selected, it.tier, it.model)
            except ConfigError as e:
                waiting.append(f"{iid} (ready, waits for model mapping)")
                attention.append(f"{iid}: {e}")
                continue
            eligible.append(iid)
            if doc.get('priority_wait', {}).get(iid):
                waiting.append(f"{iid} (ready, waits for slot taken by higher-priority plan "
                               f"{doc['priority_wait'][iid]})")
        if selected is None and reason:
            if all(readings.get(e, {}).get('verdict') == 'not-now' for e in it.engines):
                waiting.append(f"{iid} (ready, waits for budget ({', '.join(it.engines)}))")
            else:
                waiting.append(f"{iid} (ready, waits for engine: {reason})")
            for e in it.engines:
                ecap = cfg.engine_caps.get(e)
                if ecap is not None and load.get(e, 0) >= ecap:
                    holders = engine.engine_holders(plan, doc, e)
                    attention.append(f"{iid}: {e} cap {ecap} held by {', '.join(holders) or 'workers in another plan'}")
    launch = eligible
    idle = [f"{wid} ({w.get('engine', '?')})" for wid, w in doc.get("workers", {}).items()
            if w.get("worker") not in ("dead", "retired", "parked")
            and (not (w.get("pane") or w.get("session")) or w.get("worker") == "live")
            and not any(sts.get(i) in graph.ACTIVE for i in w.get("items", []))]
    unknown = [f"{wid} ({w.get('engine', '?')})" for wid, w in doc.get("workers", {}).items()
               if (w.get("pane") or w.get("session")) and w.get("worker") != "live"
               and w.get("worker") not in ("dead", "retired", "parked")
               and not any(sts.get(i) in graph.ACTIVE for i in w.get("items", []))]
    for iid, it in plan.items.items():
        if it.kind != "build" and sts[iid] == "ready":
            r = recs.get(iid, {})
            days = engine.waited_days(doc, iid, now)
            age = f" {int(days)}d" if days is not None and days >= 1 else ""
            on = f", on {it.waiting_on}{age}" if it.waiting_on else (f",{age}" if age else "")
            if it.kind == "decision":
                due, cond = engine.ask_due(plan, doc, it, sts, now)
                held = "" if due or r.get("ask") else f", held until {cond}"
                waiting.append(f"{iid} (decision{', asked ' + r['ask'] if r.get('ask') else ''}{held})")
            else:
                ok = r.get("oracle", {}).get("ok")
                asked = f", asked {r['ask']}" if r.get("ask") else ""
                waiting.append(f"{iid} (external{on}{asked}"
                               f"{', unknown' if ok is None and r.get('oracle') else ''})")
    blocked = [f"{iid}<-{','.join(d for d in plan.items[iid].depends_on if sts[d] != 'done')}"
               for iid in plan.items if sts[iid] == "blocked"]
    counts = {s: sum(1 for v in sts.values() if v == s)
              for s in ("done", "running", "review", "parked", "ready", "blocked", "abandoned")}
    return {"plan": plan.name, "date": now.date().isoformat(), "workers": [used, cap],
            "elsewhere": elsewhere,
            "counts": counts, "attention": attention, "info": info,
            "active": active, "parked": parked,
            "launchable": launch, "idle_workers": idle, "unknown_workers": unknown,
            "budgets": readings,
            "slots": max(cap - used, 0), "waiting": waiting,
            "blocked": blocked, "done": [i for i in plan.items if sts[i] == "done"],
            "states": sts}


def board(plan: Plan, doc: dict[str, Any], cfg: Config, now: dt.datetime) -> str:
    s = summary(plan, doc, cfg, now)
    c = s["counts"]
    other = sum(s["elsewhere"].values())
    split = (f" ({other} in {', '.join(f'{k} {v}' for k, v in s['elsewhere'].items())})"
             if s["elsewhere"] else "")
    head = (f"plan {s['plan']} {s['date']}  workers {s['workers'][0]}/{s['workers'][1]}{split}  "
            + " ".join(f"{k} {v}" for k, v in c.items() if v))
    out = [head]
    if cfg.budgets:
        parts = []
        for name in cfg.budgets:
            r = s['budgets'].get(name, {})
            detail = r.get('data', {}).get('figure') or r.get('why') or 'no reading yet'
            suffix = f" — {r['verdict']}" if r.get('verdict') in ('not-now', 'unknown') else ''
            parts.append(f"{name} {_short(detail, 70)}{suffix}")
        out.append('budgets: ' + ' · '.join(parts))
    if s["attention"]:
        out.append("attention:")
        out += [f"  {a}" for a in s["attention"]]
    if s["info"]:
        out.append("info:")
        out += [f"  {line}" for line in s["info"]]
    if s["active"]:
        out.append("active:")
        out += [f"  {a}" for a in s["active"]]
    if s["parked"]:
        out.append("parked: " + ", ".join(s["parked"]))
    if s["launchable"]:
        n = s["slots"]
        out.append(f"ready to launch ({n} slot{'s' if n != 1 else ''} free): "
                   + ", ".join(s["launchable"]))
    if s["idle_workers"]:
        out.append("idle workers, assignable: " + ", ".join(s["idle_workers"]))
    if s["unknown_workers"]:
        out.append("idle workers, unknown: " + ", ".join(s["unknown_workers"]))
    if s["waiting"]:
        out.append("waiting: " + ", ".join(s["waiting"]))
    if s["blocked"]:
        out.append("blocked: " + " ".join(s["blocked"]))
    if s["done"]:
        out.append("done: " + ", ".join(s["done"]))
    return "\n".join(out) + "\n"


def show(plan: Plan, doc: dict[str, Any], iid: str, events: list[dict[str, Any]],
         cfg: Config | None = None) -> str:
    it = plan.items[iid]
    r = doc.get("items", {}).get(iid, {})
    st = graph.states(plan, doc)[iid]
    lines = [f"{iid}: {it.title}", f"  kind {it.kind}, state {st}"
             + (f", ref {it.ref}" if it.ref else "")]
    if it.repo:
        lines.append(f"  repo {it.repo}  topic {it.topic}")
    if it.kind == "build":
        current_launch = (st in graph.ACTIVE and r.get("worker") != "dead") or st == "parked"
        lines.append(f"  engine {r.get('engine') if current_launch and r.get('engine') else ', '.join(it.engines)}")
        if current_launch:
            lines.append(f"  tier {r.get('tier') or 'default'}, model {r.get('model') or 'default'}"
                         + (f", reasoning {r['reasoning_effort']}" if r.get('reasoning_effort') else ""))
        elif cfg is not None:
            for name in it.engines:
                try:
                    spec = cfg.model_for(name, it.tier, it.model)
                except ConfigError:
                    lines.append(f"  tier {it.tier}, model unconfigured ({name})")
                    continue
                lines.append(f"  tier {it.tier or 'default'}, model {spec.model or 'default'}"
                             + (f", reasoning {spec.reasoning_effort}" if spec.reasoning_effort else "")
                             + (f" ({name})" if len(it.engines) > 1 else ""))
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
    for k in ("wid", "session", "pane", "worktree", "worker", "launched", "last_report",
              "last_line", "stuck", "waiting", "wait_target", "parked", "park_refusal", "stale", "reopened", "ask", "note"):
        if r.get(k):
            lines.append(f"  {k} {r[k]}")
    for e in r.get("evidence", [])[-3:]:
        pins = ", ".join(f"{k}={v}" for k, v in e.get("pins", {}).items())
        lines.append(f"  evidence {e['at']} ({e['by']}): {e['ref']}" + (f" at {pins}" if pins else ""))
    if r.get("oracle"):
        lines.append(f"  oracle said {r['oracle'].get('ok')} at {r['oracle'].get('checked')}: "
                     f"{r['oracle'].get('detail')}")
    if events:
        lines.append("  recent events:")
        for e in events[-8:]:
            rest = {k: v for k, v in e.items() if k not in ("t", "item", "what")}
            lines.append(f"    {e['t']} {e['what']} {_short(str(rest), 90)}")
    return "\n".join(lines) + "\n"
