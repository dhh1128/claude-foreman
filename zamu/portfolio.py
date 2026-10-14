"""Read-only, compact machine view over every plan."""

from __future__ import annotations

import json
import math
import tomllib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import budget_probes, budgets, config, engine, graph, procs, store
from .config import Config
from .engine import World
from .plan import Plan, PlanError, load


def _session_files(root: Path) -> dict[str, Path] | None:
    if not root.is_dir():
        return None
    try:
        return {p.stem: p for p in root.rglob("*.jsonl") if p.is_file()}
    except OSError:
        return None


def _claude_spend(path: Path, factors: dict | None = None, launched: str = "") -> float | None:
    seen = set()
    total = 0.0
    found = False
    since = engine._parse(launched)
    try:
        with path.open(errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    message = row.get("message", {})
                    usage = message.get("usage")
                    mid = message.get("id")
                    if not isinstance(usage, dict) or not mid or mid in seen:
                        continue
                    if since:
                        at = engine._parse(row.get("timestamp", "").replace("Z", "+00:00"))
                        if at is None or at < since:
                            continue
                    seen.add(mid)
                    total += budget_probes.weighted_usage(message, factors)
                    found = True
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        return None
    return total if found else None


def _codex_spend(path: Path) -> float | None:
    total = None
    try:
        with path.open(errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    payload = row.get("payload", {})
                    if payload.get("type") != "token_count":
                        continue
                    usage = (payload.get("info") or {}).get("total_token_usage") or {}
                    amount = usage.get("total_tokens")
                    if isinstance(amount, (int, float)):
                        total = max(total or 0, amount)
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        return None
    return total


def _codex_rollout(files: dict[str, Path] | None, worktree: str, launched: str) -> Path | None:
    """Codex launches have a wrapper token, but Codex chooses its own rollout id."""
    if files is None or not worktree or not launched:
        return None
    since = engine._parse(launched)
    if since is None:
        return None
    matches = []
    for path in files.values():
        try:
            with path.open(errors="replace") as fh:
                row = json.loads(next(fh))
            meta = row.get("payload", {}) if row.get("type") == "session_meta" else {}
            at = engine._parse(meta.get("timestamp", "").replace("Z", "+00:00"))
            if meta.get("cwd") == worktree and at and at >= since:
                matches.append((at, path))
        except (OSError, StopIteration, ValueError, AttributeError):
            continue
    return min(matches)[1] if matches else None


def _workers(plan: Plan, doc: dict, sts: dict[str, str]) -> list[dict]:
    out = {}
    for iid, rec in doc.get("items", {}).items():
        if iid not in plan.items or sts[iid] not in graph.ACTIVE or rec.get("worker") == "dead":
            continue
        if not (rec.get("session") or rec.get("pane") or rec.get("wid")):
            continue
        wid = rec.get("wid") or iid
        if wid not in out:
            out[wid] = {"id": wid, "engine": rec.get("engine") or plan.items[iid].engines[0],
                        "session": rec.get("session", ""), "token": rec.get("token", ""),
                        "worktree": rec.get("worktree", ""), "launched": rec.get("launched", "")}
    return list(out.values())


def _resources(workers: list[tuple[str, dict]], ps: list[procs.Proc] | None) -> dict[tuple[str, str], tuple[int | None, float | None]]:
    result = {(plan, w["id"]): (None, None) for plan, w in workers}
    if ps is None:
        return result
    identities = defaultdict(set)
    paths = defaultdict(set)
    for plan, w in workers:
        key = (plan, w["id"])
        identities[w["id"]].add(key)
        if w["token"]:
            identities[w["token"]].add(key)
        if w["session"]:
            identities[w["session"]].add(key)
        if w["worktree"]:
            paths[w["worktree"]].add(key)
    by_pid = {p.pid: p for p in ps}
    amounts = defaultdict(lambda: [0, 0.0, 0])
    for proc in ps:
        p, seen, key, path_owner, ambiguous = proc, set(), None, None, set()
        while p.pid not in seen:
            seen.add(p.pid)
            candidates = identities.get(procs.worker_id(p), set())
            if len(candidates) == 1:
                key = next(iter(candidates))
                break
            if candidates and not ambiguous:
                ambiguous = candidates
            if path_owner is None:
                matched = [owners for root, owners in paths.items() if procs.under(p.path, root)]
                if matched:
                    owners = set().union(*matched)
                    path_owner = next(iter(owners)) if len(owners) == 1 else None
            p = by_pid.get(p.ppid)
            if p is None:
                break
        if key is None:
            key = path_owner if not ambiguous or path_owner in ambiguous else None
        if key:
            a = amounts[key]
            a[0] += proc.rss_bytes
            a[1] += proc.cpu_pct
            a[2] += 1
    for key, (rss, cpu, count) in amounts.items():
        if count:
            result[key] = (rss, round(cpu, 1))
    return result


def _downstream(plan: Plan, sts: dict[str, str], iid: str) -> set[str]:
    seen, todo = set(), [iid]
    while todo:
        for child in plan.dependents(todo.pop()):
            if child not in seen and sts[child] not in graph.FINAL:
                seen.add(child)
                todo.append(child)
    return seen


def _descendants(plan: Plan, sts: dict[str, str], iid: str) -> int:
    return len(_downstream(plan, sts, iid))


def _claude_settings() -> tuple[Path, dict | None]:
    path = Path("~/.config/budget/claude.toml").expanduser()
    try:
        doc = tomllib.loads(path.read_text())
    except FileNotFoundError:
        doc = {}
    except (OSError, ValueError):
        return Path("~/.claude/projects").expanduser(), None
    projects = doc.get("projects", "~/.claude/projects")
    if not isinstance(projects, str):
        return Path("~/.claude/projects").expanduser(), None
    factors = doc.get("model_factors", {})
    if not isinstance(factors, dict) or any(k not in ("opus", "sonnet", "haiku") or
                                                 not isinstance(v, (int, float)) or
                                                 not math.isfinite(v) or v <= 0
                                                 for k, v in factors.items()):
        return Path(projects).expanduser(), None
    return Path(projects).expanduser(), factors


def snapshot(cfg: Config, world: World, *, claude_projects: Path | None = None,
             codex_sessions: Path | None = None, claude_factors: dict | None = None) -> dict[str, Any]:
    now = world.now()
    entries, errors, unparsed = [], [], []
    root = config.plans_dir()
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if not (d / "plan.toml").exists():
            continue
        try:
            plan = load(d / "plan.toml", name=d.name)
            doc = store.read(d)
            sts = graph.states(plan, doc)
            entries.append((plan, doc, sts, _workers(plan, doc, sts)))
        except (PlanError, store.StoreError, OSError, ValueError) as e:
            errors.append({"name": d.name, "error": str(e)})
            try:
                unparsed.extend((d.name, rec) for rec in store.read(d).get("items", {}).values()
                                if rec.get("state") in graph.ACTIVE and rec.get("worker") != "dead"
                                and (rec.get("session") or rec.get("pane") or rec.get("wid")))
            except (OSError, store.StoreError):
                pass
    all_workers = [(plan.name, w) for plan, _doc, _sts, ws in entries for w in ws]
    resources = _resources(all_workers, world.scan())
    configured_projects, configured_factors = _claude_settings() if claude_projects is None or claude_factors is None else (None, None)
    factors = claude_factors if claude_factors is not None else configured_factors
    claude_files = _session_files(claude_projects or configured_projects or Path.home() / ".claude/projects")
    codex_files = _session_files(codex_sessions or Path.home() / ".codex/sessions")
    try:
        rc, out, _err = world.run(["systemctl", "--user", "is-active", "zamu-watch"])
        watcher = out.strip() if rc in (0, 3) and out.strip() else "?"
    except Exception:
        watcher = "?"
    admission, why = engine.admit(cfg, world)
    saved = budgets.read() if cfg.budgets else {}
    readings = {e: (saved or {}).get(e, {"verdict": "unknown", "why": "no reading yet"}) for e in cfg.budgets}
    plans, waits, active_by_repo, pins = [], [], defaultdict(list), []
    for plan, doc, sts, ws in entries:
        counts = dict(Counter(sts.values()))
        by_engine = dict(Counter(w["engine"] for w in ws))
        rss = cpu = 0
        spent = defaultdict(float)
        unknown_rss = False
        unknown_spent = Counter()
        measured_spend = set()
        for w in ws:
            r, c = resources[(plan.name, w["id"])]
            if r is None:
                unknown_rss = True
            else:
                rss += r
                cpu += c
            sid = w["session"]
            files = claude_files if w["engine"] == "claude" else codex_files if w["engine"] == "codex" else None
            path = files.get(sid) if files is not None and sid else None
            if path is None and files is not None and sid and w["engine"] == "codex":
                path = next((v for k, v in files.items() if k.endswith(sid)), None)
            if path is None and w["engine"] == "codex":
                path = _codex_rollout(codex_files, w["worktree"], w["launched"])
            amount = (_claude_spend(path, factors, w["launched"]) if w["engine"] == "claude" and factors is not None
                      else _codex_spend(path) if w["engine"] == "codex" else None) if path else None
            if amount is None:
                unknown_spent[w["engine"]] += 1
            else:
                measured_spend.add(w["engine"])
                spent[w["engine"]] += amount
        for iid, it in plan.items.items():
            rec = doc.get("items", {}).get(iid, {})
            if sts[iid] in graph.ACTIVE:
                for repo in it.repos:
                    active_by_repo[repo].append(f"{plan.name}/{iid}")
            if sts[iid] in graph.FINAL:
                continue
            ask = rec.get("ask")
            text = None
            if rec.get("waiting"):
                text = rec["waiting"]
            elif (it.kind == "decision" and (ask or (sts[iid] == "ready" and
                  engine.ask_due(plan, doc, it, sts, now)[0]))):
                text = it.question or it.title
            elif (it.kind == "external" and engine.waits_on_user(it) and
                  (ask or sts[iid] == "ready")):
                text = it.question or it.title
            if text:
                waits.append({"id": f"{plan.name}/{iid}", "ask": ask, "text": text,
                              "unblocks": _descendants(plan, sts, iid)})
        for name, pin in plan.pins.items():
            if pin.repo:
                pins.append((plan.name, name, pin.repo))
        episode = doc.get("budget_episode", {})
        if episode.get("ask") and episode.get("ask") != "board":
            held = set()
            for iid in episode.get("waiting", []):
                if iid in plan.items and sts[iid] not in graph.FINAL:
                    held.add(iid)
                    held.update(_downstream(plan, sts, iid))
            waits.append({"id": f"{plan.name}/budget", "ask": episode["ask"],
                          "text": episode.get("line") or "Choose a budget trade-off",
                          "unblocks": len(held)})
        if pm_ask := doc.get("pm", {}).get("dead_ask"):
            waits.append({"id": f"{plan.name}/pm", "ask": pm_ask,
                          "text": f"Restart the {plan.name} PM",
                          "unblocks": sum(st not in graph.FINAL for st in sts.values())})
        risk = graph.risks(plan, sts, now.date())
        pm = doc.get("pm", {})
        status = "?"
        if pm.get("repo") and pm.get("target"):
            live = world.coord.live_target(pm["repo"], pm["target"])
            status = "live" if live is True else "dead" if live is False else "?"
        plans.append({"name": plan.name, "counts": counts, "pm": status,
                      "autolaunch": plan.autolaunch,
                      "risk": {"item": risk[0].item, "over_days": risk[0].over_days,
                               "due": risk[0].due.isoformat(), "chain": risk[0].chain} if risk else None,
                      "waits": sum(w["id"].startswith(plan.name + "/") for w in waits),
                      "workers": by_engine, "slots": len(ws), "share_pct": round(100 * len(ws) / cfg.max_workers),
                      "rss_bytes": None if unknown_rss else rss,
                      "cpu_pct": None if unknown_rss else round(cpu, 1),
                      "spent": {e: round(spent[e]) if e in measured_spend else None
                                for e in by_engine},
                      "spent_unknown": dict(unknown_spent),
                      "last_tick": doc.get("watch", {}).get("last_tick")})
    plans.sort(key=lambda p: (-p["slots"], -(p["rss_bytes"] or 0), p["name"]))
    waits.sort(key=lambda w: (-w["unblocks"], w["id"]))
    conflicts = []
    for repo, ids in active_by_repo.items():
        if len({i.split("/", 1)[0] for i in ids}) > 1:
            conflicts.append({"kind": "repo", "repo": repo, "items": ids})
    for owner, name, repo in pins:
        others = [i for i in active_by_repo.get(repo, []) if not i.startswith(owner + "/")]
        if others:
            conflicts.append({"kind": "pin", "pin": f"{owner}/{name}", "repo": repo, "items": others})
    extra = {(name, rec.get("wid") or rec.get("session") or rec.get("pane")):
             rec.get("engine") or "claude" for name, rec in unparsed}
    workers = dict(Counter([w["engine"] for _p, w in all_workers] + list(extra.values())))
    return {"box": {"workers": workers, "used": len(all_workers) + len(extra), "cap": cfg.max_workers,
                    "engine_caps": cfg.engine_caps, "admission": admission, "admission_why": why,
                    "budgets": readings, "watcher": watcher}, "plans": plans, "waiting": waits,
            "conflicts": conflicts, "errors": errors}


def board(data: dict[str, Any]) -> str:
    box = data["box"]
    engines = ",".join(f"{e} {box['workers'].get(e, 0)}/{box['engine_caps'].get(e, '∞')}"
                       for e in sorted(box["workers"].keys() | box["engine_caps"].keys())) or "none"
    verdict = "yes" if box["admission"] is True else "no" if box["admission"] is False else "?"
    lines = [f"box {box['used']}/{box['cap']} slots ({engines}); admit {verdict} {box['admission_why']}; watcher {box['watcher']}"]
    if box["budgets"]:
        lines.append("budgets " + " ".join(f"{e}:{r.get('verdict', '?')}" for e, r in box["budgets"].items()))
    for p in data["plans"]:
        labels = {"done": "Done", "running": "Run", "review": "Rev", "ready": "Ready",
                  "blocked": "Blocked", "abandoned": "Abandoned"}
        counts = ",".join(f"{labels[s]}{n}" for s, n in p["counts"].items() if n)
        held = ",".join(f"{e}:{n}" for e, n in sorted(p["workers"].items())) or "none"
        rss = "?" if p["rss_bytes"] is None else f"{p['rss_bytes'] / 1048576:.0f}M"
        cpu = "?" if p["cpu_pct"] is None else f"{p['cpu_pct']:g}%"
        spent = ",".join(f"{e} {'?' if amount is None else '~' + str(amount) + ('wtok' if e == 'claude' else 'tok') + ('+?' if p['spent_unknown'].get(e) else '')}"
                         for e, amount in sorted(p["spent"].items())) or "-"
        tick = p["last_tick"][:16] if p["last_tick"] else "?"
        risk = p["risk"]
        risk_text = f"{risk['item']}+{risk['over_days']}d" if risk else "-"
        lines.append(f"{p['name']} {p['slots']}/{box['cap']} ({p['share_pct']}%) {held} RSS {rss} CPU {cpu} spent {spent}; {counts}; PM {p['pm']} auto {'on' if p['autolaunch'] else 'off'} risk {risk_text} waits {p['waits']} tick {tick}")
    if data["waiting"]:
        def short(text: str) -> str:
            text = " ".join(text.split())
            return text if len(text) <= 55 else text[:54] + "…"
        lines.append("waiting: " + "; ".join(f"{w['id']} {w['ask'] or '-'} (+{w['unblocks']}) {short(w['text'])}" for w in data["waiting"]))
    if data["conflicts"]:
        lines.append("conflicts: " + "; ".join(f"{c['kind']} {c.get('pin') or c['repo']}: {','.join(c['items'])}" for c in data["conflicts"]))
    lines.extend(f"{e['name']}: could not load ({e['error']})" for e in data["errors"])
    return "\n".join(lines) + "\n"
