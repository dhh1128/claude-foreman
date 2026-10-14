"""Read-only, compact machine view over every plan."""

from __future__ import annotations

import json
import math
import re
import subprocess
import tomllib
import datetime as dt
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import budget_probes, budgets, config, engine, feedback, friction, graph, procs, store
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


def _window(doc: dict, name: str) -> str:
    session = doc.get("pm", {}).get("tmux_session") or "?"
    return f"{session}:{name}"


def _pane_window(pane: str, world: World, cache: dict[str, str]) -> str:
    if pane and pane not in cache:
        found = _look(world, ["tmux", "display-message", "-p", "-t", pane,
                              "#{session_name}:#{window_name}"])
        cache[pane] = found if found and re.fullmatch(r"[^:\n]+:[^:\n]+", found) else "?"
    return cache.get(pane, "?")


def _pm_window(doc: dict, world: World, cache: dict[str, str]) -> str:
    pm = doc.get("pm", {})
    live = _pane_window(pm.get("pane") or "", world, cache)
    if live != "?":
        return live
    target = pm.get("target") or ""
    if not target:
        return "?"
    session = pm.get("tmux_session") or ""
    return f"{session}:{target}" if session and ":" not in target and not target.startswith("%") else target


def _worker_window(doc: dict, it, rec: dict, world: World, cache: dict[str, str]) -> str:
    pane = rec.get("pane") or ""
    live = _pane_window(pane, world, cache)
    if live != "?":
        return live
    return _window(doc, it.topic) if rec.get("ours") else "?"


def _look(world: World, argv: list[str]) -> str | None:
    """A command's output, or None when the world could not be inspected."""
    try:
        rc, out, _ = world.run(argv)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return out.strip() if rc == 0 else None


def _repo_slug(remote: str | None) -> str | None:
    if remote is None:
        return None
    match = re.fullmatch(r"(?:https://|ssh://git@|git@)github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?", remote)
    return match.group(1) if match else None


def _origin_default(world: World, repo: str) -> str | None:
    ref = _look(world, ["git", "-C", repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if ref and ref.startswith("origin/"):
        return ref
    for name in ("main", "master"):
        ref = f"refs/remotes/origin/{name}"
        if _look(world, ["git", "-C", repo, "rev-parse", "--verify", "--quiet", ref]):
            return f"origin/{name}"
    return None


def _ci(checks: Any) -> str:
    if not isinstance(checks, list):
        return "?"
    if not checks:
        return "none"
    states = []
    for check in checks:
        if not isinstance(check, dict):
            return "?"
        value = str(check.get("conclusion") or check.get("state") or check.get("status") or "").upper()
        if value in ("FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED"):
            states.append("fail")
        elif value in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            states.append("pass")
        elif value:
            states.append("pending")
        else:
            states.append("?")
    return "fail" if "fail" in states else "pending" if "pending" in states else "?" if "?" in states else "pass"


def _pr(world: World, worktree: str, topic: str) -> tuple[str, str, str]:
    remote = _look(world, ["git", "-C", worktree, "remote", "get-url", "origin"])
    slug = _repo_slug(remote)
    if slug is None:
        return "?", "?", "?"
    raw = _look(world, ["gh", "pr", "list", "--repo", slug, "--head", topic,
                        "--state", "all", "--json", "url,state,statusCheckRollup", "--limit", "1"])
    if raw is None:
        return "?", "?", "?"
    try:
        rows = json.loads(raw)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return "?", "?", "?"
        if not rows:
            return "none", "", ""
        row = rows[0]
        return str(row.get("url") or "?"), str(row.get("state") or "?").lower(), _ci(row.get("statusCheckRollup"))
    except (ValueError, TypeError):
        return "?", "?", "?"


def _session_model(path: Path | None, engine_name: str) -> str | None:
    if path is None:
        return None
    model = None
    try:
        with path.open(errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    payload = row.get("message", {}) if engine_name == "claude" else row.get("payload", {})
                    candidate = payload.get("model") if isinstance(payload, dict) else None
                    if isinstance(candidate, str) and candidate and candidate != "<synthetic>":
                        model = candidate
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        return None
    return model


def _model(cfg: Config, engine_name: str, rec: dict, log_model: str | None) -> str:
    if rec.get("model"):
        return str(rec["model"])
    if log_model:
        return log_model
    argv = cfg.engines.get(engine_name, ())
    for n, arg in enumerate(argv):
        if arg in ("-m", "--model") and n + 1 < len(argv):
            return argv[n + 1]
        if arg.startswith("--model="):
            return arg.partition("=")[2]
    return "?"


def _worker_detail(plan: Plan, it, rec: dict, doc: dict, cfg: Config, world: World,
                   now: dt.datetime, log_model: str | None, windows: dict[str, str]) -> dict:
    worktree = rec.get("worktree") or f"{it.repo.rstrip('/')}/.worktrees/{it.topic}"
    branch = _look(world, ["git", "-C", worktree, "symbolic-ref", "--short", "HEAD"])
    base = it.base or _origin_default(world, worktree)
    if base and not base.startswith("origin/"):
        base = "origin/" + base
    fork = _look(world, ["git", "-C", worktree, "merge-base", "HEAD", base]) if base else None
    commits = _look(world, ["git", "-C", worktree, "rev-list", "--count", f"{fork}..HEAD"]) if fork else None
    changed = _look(world, ["git", "-C", worktree, "diff", "--name-only", "-z", fork, "--"]) if fork else None
    untracked = _look(world, ["git", "-C", worktree, "ls-files", "--others", "--exclude-standard", "-z"])
    files = (len((set(changed.split("\0")) | set(untracked.split("\0"))) - {""})
             if changed is not None and untracked is not None else None)
    pr, pr_state, ci = _pr(world, worktree, branch or it.topic)
    launched = engine._parse(rec.get("launched", ""))
    age_hours = max(0, int((now - launched).total_seconds() // 3600)) if launched else None
    return {"id": f"{plan.name}/{it.id}", "engine": rec.get("engine") or it.engines[0],
            "model": _model(cfg, rec.get("engine") or it.engines[0], rec, log_model),
            "window": _worker_window(doc, it, rec, world, windows), "age_hours": age_hours,
            "last_report": rec.get("last_report") or "?", "last_line": rec.get("last_line") or "?",
            "commits": int(commits) if commits and commits.isdigit() else None,
            "files": files, "pr": pr, "pr_state": pr_state, "ci": ci}


def _landed(plan: Plan, doc: dict, sts: dict[str, str], now: dt.datetime, world: World) -> list[dict] | None:
    try:
        events = store.events(plan.path.parent)
    except (OSError, store.StoreError):
        return None
    midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    completed = set()
    for ev in events:
        at = engine._parse(ev.get("t", ""))
        iid = ev.get("item")
        if iid not in plan.items or sts[iid] != "done" or at is None or at.astimezone() < midnight:
            continue
        if (ev.get("what") == "oracle" and ev.get("ok") is True) or \
           (ev.get("what") == "mark" and ev.get("state") == "done") or \
           (ev.get("what") == "report" and " done; oracle agrees" in ev.get("outcome", "")) or \
           (ev.get("what") == "verify" and ev.get("ok") is True):
            completed.add(iid)
    out = []
    for iid in sorted(completed):
        it = plan.items[iid]
        rec = doc.get("items", {}).get(iid, {})
        reference = next((f"https://github.com/{spec[3:].replace('#', '/pull/')}" for spec in it.oracle if spec.startswith("pr:")), "")
        if not reference:
            reports = (ev.get("line", "") for ev in reversed(events)
                       if ev.get("item") == iid and ev.get("what") == "report" and ev.get("state") == "review")
            for line in (rec.get("last_line", ""), *reports):
                match = re.search(r"https://[^\s)]+/pull/\d+", line)
                if match:
                    reference = match.group(0)
                    break
        if not reference and it.repo:
            default = _origin_default(world, it.repo)
            branch = (_look(world, ["git", "-C", it.repo, "rev-parse", "--verify", f"refs/heads/{it.topic}"])
                      or _look(world, ["git", "-C", it.repo, "rev-parse", "--verify", f"refs/remotes/origin/{it.topic}"]))
            if default and branch:
                try:
                    rc, _, _ = world.run(["git", "-C", it.repo, "merge-base", "--is-ancestor", branch, default])
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    rc = None
                if rc == 0:
                    reference = branch[:7]
        out.append({"id": f"{plan.name}/{iid}", "reference": reference or "?"})
    return out


def snapshot(cfg: Config, world: World, *, claude_projects: Path | None = None,
             codex_sessions: Path | None = None, claude_factors: dict | None = None,
             dashboard: bool = False) -> dict[str, Any]:
    now = world.now()
    entries, errors, unparsed, plan_dirs = [], [], [], []
    root = config.plans_dir()
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if not (d / "plan.toml").exists():
            continue
        plan_dirs.append(d)
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
    detail = {"now": now.isoformat(), "workers": [], "risks": [], "landed": [], "landed_unknown": []}
    for plan, doc, sts, ws in entries:
        windows: dict[str, str] = {}
        counts = dict(Counter(sts.values()))
        by_engine = dict(Counter(w["engine"] for w in ws))
        rss = cpu = 0
        spent = defaultdict(float)
        unknown_rss = False
        unknown_spent = Counter()
        measured_spend = set()
        log_models = {}
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
            if dashboard:
                log_models[w["id"]] = _session_model(path, w["engine"])
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
                destination = (_worker_window(doc, it, rec, world, windows) if dashboard and
                               (rec.get("wid") or rec.get("pane") or rec.get("session")) and rec.get("worker") != "dead"
                               else _pm_window(doc, world, windows) if dashboard else _window(doc, f"{plan.name}-prj"))
                waits.append({"id": f"{plan.name}/{iid}", "ask": ask, "text": text,
                              "unblocks": _descendants(plan, sts, iid),
                              "window": destination})
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
                          "unblocks": len(held), "window": _pm_window(doc, world, windows) if dashboard else _window(doc, f"{plan.name}-prj")})
        if pm_ask := doc.get("pm", {}).get("dead_ask"):
            waits.append({"id": f"{plan.name}/pm", "ask": pm_ask,
                          "text": f"Restart the {plan.name} PM",
                          "unblocks": sum(st not in graph.FINAL for st in sts.values()),
                          "window": _pm_window(doc, world, windows) if dashboard else _window(doc, f"{plan.name}-prj")})
        risk = graph.risks(plan, sts, now.date())
        if dashboard:
            for r in risk:
                detail["risks"].append({"id": f"{plan.name}/{r.item}", "over_days": max(r.over_days, (now.date() - r.due).days),
                                        "due": r.due.isoformat(), "chain": r.chain, "overdue": r.overdue})
            for iid, it in plan.items.items():
                rec = doc.get("items", {}).get(iid, {})
                if sts[iid] in graph.ACTIVE and rec.get("worker") != "dead" and (rec.get("wid") or rec.get("pane") or rec.get("session")):
                    detail["workers"].append(_worker_detail(plan, it, rec, doc, cfg, world, now,
                                                             log_models.get(rec.get("wid") or iid), windows))
            landed = _landed(plan, doc, sts, now, world)
            if landed is None:
                detail["landed_unknown"].append(plan.name)
            else:
                detail["landed"].extend(landed)
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
    friction_data = friction.totals(plan_dirs, now)
    try:
        friction_data["feedback"] = feedback.list_feedback(limit=3)
    except (store.StoreError, OSError, TimeoutError):
        friction_data["feedback"] = None
    result = {"box": {"workers": workers, "used": len(all_workers) + len(extra), "cap": cfg.max_workers,
                    "engine_caps": cfg.engine_caps, "admission": admission, "admission_why": why,
                    "budgets": readings, "watcher": watcher}, "plans": plans, "waiting": waits,
            "conflicts": conflicts, "errors": errors, "friction": friction_data}
    if dashboard:
        result["dashboard"] = detail
    return result


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
    if fr := data.get("friction"):
        labels = {"adopted": "adopted", "revived": "revived", "admission_holds": "admission holds",
                  "admission_hours": "admission hours", "budget_holds": "budget holds",
                  "budget_hours": "budget hours", "nagged_waits": "past nag", "pm_reports": "PM reports",
                  "verify_reopens": "verify reopens"}
        current, previous = fr["this_week"], fr["previous_week"]
        counts = [f"{label} {current[key]:g} ({current[key] - previous[key]:+g})"
                  for key, label in labels.items() if current[key] or previous[key]]
        lines.append("friction this week: " + ("; ".join(counts) if counts else "none") +
                     ("; event log ?" if fr["unknown"] else ""))
        if fr["feedback"] is None:
            lines.append("feedback: ?")
        for row in fr["feedback"] or []:
            text = " ".join(row.get("text", "").split())
            lines.append(f"feedback {row['t'][:10]} {row.get('plan') or '-'}: {text[:90]}{'…' if len(text) > 90 else ''}")
    lines.extend(f"{e['name']}: could not load ({e['error']})" for e in data["errors"])
    return "\n".join(lines) + "\n"


def dashboard(data: dict[str, Any], scope: str | None) -> str:
    """Daniel's on-demand terminal view; scope is one plan or None for all plans."""
    detail = data["dashboard"]
    selected = lambda identifier: scope is None or identifier.startswith(scope + "/")
    lines = [f"dashboard {scope or 'all'} ({detail['now'][:16]})"]
    labels = {"done": "done", "running": "running", "review": "review", "ready": "ready", "blocked": "blocked", "abandoned": "abandoned"}
    for p in data["plans"]:
        counts = ", ".join(f"{n} {labels[s]}" for s, n in p["counts"].items() if n)
        lines.append(f"{p['name']}: {counts}; PM {p['pm']}; {p['waits']} waiting on Daniel")
    waits = sorted((w for w in data["waiting"] if selected(w["id"])),
                   key=lambda w: (-w["unblocks"], w["id"]))
    lines.append("Waiting on Daniel:")
    lines.extend(f"  {w['id']} {w['ask'] or '-'} (+{w['unblocks']}) {w['text']} — {w['window']}" for w in waits)
    if not waits:
        lines.append("  none")
    workers = [w for w in detail["workers"] if selected(w["id"])]
    lines.append("Running:")
    for w in workers:
        age = f"{w['age_hours']}h" if w["age_hours"] is not None else "?"
        commits = f"{w['commits']} commit{'s' if w['commits'] != 1 else ''}" if w["commits"] is not None else "commits ?"
        files = f"{w['files']} file{'s' if w['files'] != 1 else ''}" if w["files"] is not None else "files ?"
        pr = f"{w['pr']} ({w['pr_state']}, CI {w['ci']})" if w["pr"] not in ("?", "none") else w["pr"]
        reported = w["last_report"][:16]
        lines.append(f"  {w['id']} {w['engine']}/{w['model']} {w['window']} age {age}; reported {reported}: {w['last_line']}; {commits}, {files}; PR {pr}")
    if not workers:
        lines.append("  none")
    risks = [r for r in detail["risks"] if selected(r["id"])]
    lines.append("Late:")
    lines.extend(f"  {r['id']} {r['over_days']}d over ({r['due']}): {' → '.join(r['chain'])}" for r in risks)
    if not risks:
        lines.append("  none")
    landed = [r for r in detail["landed"] if selected(r["id"])]
    lines.append("Landed today:")
    lines.extend(f"  {r['id']} {r['reference']}" for r in landed)
    unknown = [name for name in detail["landed_unknown"] if scope is None or name == scope]
    lines.extend(f"  {name}: completion events unreadable" for name in unknown)
    if not landed and not unknown:
        lines.append("  none")
    lines.extend(f"{e['name']}: could not load ({e['error']})" for e in data["errors"])
    return "\n".join(lines) + "\n"
