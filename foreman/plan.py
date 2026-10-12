"""The plan: a dependency graph of work items across repos, written by hand in TOML.

The plan file is the human's half of the state. It says what the work is and what depends on
what. It never says what has happened; that is `state.json`, beside it, which only the CLI
writes. Keeping the two apart means a person can edit the plan with the PM running, and the
next `pulse` picks the edit up without either side overwriting the other.

A plan that cannot be trusted is refused at load rather than half-used: an unknown kind, a
dependency naming nothing, a cycle, two items wanting one worktree. Every one of those would
otherwise surface later as a worker launched into the wrong place, or an item that is never
ready and never says why.

Pure apart from `load`, which reads one file.
"""

from __future__ import annotations

import datetime as dt
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from . import oracles

KINDS = ("build", "decision", "external")
#: States a plan file may pin by hand. The others are computed or recorded by the CLI.
PINNABLE = ("done", "abandoned")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class PlanError(ValueError):
    """The plan cannot be used as written. The message names the item and the fix."""


@dataclass(frozen=True)
class Item:
    id: str
    title: str
    kind: str
    repo: str = ""           # absolute path; required for build items
    topic: str = ""          # worktree and branch name; defaults to the id
    depends_on: tuple[str, ...] = ()
    oracle: tuple[str, ...] = ()   # every spec must hold; empty only for decisions
    brief: str = ""          # absolute path, resolved against the plan's directory
    question: str = ""       # for decisions: the question, phrased to stand on its own
    due: dt.date | None = None
    due_own: bool = False    # False when `due` was inherited from [plan]
    estimate_days: float = 0.0
    pinned: str = ""         # "done" or "abandoned" when the human says so in the file
    ref: str = ""            # free text: a tracker id, an issue, a PR -- shown, never parsed
    base: str = ""           # branch to start the worktree from; default origin's HEAD
    engine: str = "claude"   # what `launch` starts: a key of config `engines`
    rests_on: tuple[str, ...] = ()   # pins whose movement makes this item's evidence stale
    #: Other repos this item's work lands in. `repo` is where its worker starts; a change that
    #: spans a test suite and a library touches both, and merges queue in each.
    touches: tuple[str, ...] = ()
    #: Who an item is waiting on, for the board: "user" (the human running the plan), or a name
    #: such as "Sam Smith". An external item waiting on "user" files an ask to go and do it.
    waiting_on: str = ""
    #: Raise an attention line once the item has been ready but unfinished this long.
    nag_after_days: float = 0.0
    #: For a decision: hold the ask until this holds. "<item> waits > 21d", or a date.
    ask_when: str = ""

    @property
    def repos(self) -> tuple[str, ...]:
        return tuple(r.rstrip("/") for r in (self.repo, *self.touches) if r)


@dataclass(frozen=True)
class Pin:
    """Something the plan's evidence rests on: a commit, a version, a lockfile line.

    `cmd` prints its current value. When the value moves, every done item that rests on it is
    flagged stale until it is verified again. After `freeze`, any movement is itself flagged.
    """
    name: str
    cmd: str
    repo: str = ""           # where cmd runs; default the plan's directory
    freeze: dt.date | None = None


@dataclass(frozen=True)
class Plan:
    name: str
    path: Path
    items: Mapping[str, Item]
    common_brief: str = ""
    due: dt.date | None = None
    max_workers: int | None = None
    #: Standing rules for this plan, set by the user mid-run; the PM reads and applies them.
    rules: str = ""
    #: Documents the PM itself maintains -- coordination work, as opposed to the items' work.
    owns: tuple[str, ...] = ()
    pins: Mapping[str, Pin] = field(default_factory=dict)

    def dependents(self, item_id: str) -> list[str]:
        return [i.id for i in self.items.values() if item_id in i.depends_on]


def _date(v: Any, where: str) -> dt.date | None:
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v))
    except ValueError:
        raise PlanError(f"{where}: due {v!r} is not a date; write it as 2026-11-17.") from None


def _days(v: Any, where: str) -> float:
    """`estimate` as a number of days, or a string like "3d", "2w", "6h"."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([hdw]?)\s*", str(v))
    if not m:
        raise PlanError(f"{where}: estimate {v!r} is not a duration; write 3d, 2w or 6h.")
    n, unit = float(m.group(1)), m.group(2) or "d"
    return {"h": n / 24, "d": n, "w": n * 7}[unit]


def _strs(v: Any, where: str, key: str) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,) if v.strip() else ()
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return tuple(x for x in v if x.strip())
    raise PlanError(f"{where}: {key} must be a string or a list of strings.")


def _path(v: str, base: Path) -> str:
    if not v:
        return ""
    p = Path(v).expanduser()
    return str(p if p.is_absolute() else (base / p))


def parse(text: str, path: Path, name: str | None = None) -> Plan:
    """Build a Plan from TOML text, refusing anything that would mislead later."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise PlanError(f"{path}: not valid TOML: {e}") from None
    base = path.parent
    meta = doc.get("plan", {})
    if not isinstance(meta, dict):
        raise PlanError(f"{path}: [plan] must be a table.")
    raw_items = doc.get("items", {})
    if not isinstance(raw_items, dict) or not raw_items:
        raise PlanError(f"{path}: no [items.<id>] tables; a plan needs at least one item.")
    unknown_top = set(doc) - {"plan", "items", "pins"}
    if unknown_top:
        raise PlanError(f"{path}: unknown top-level keys {sorted(unknown_top)}; "
                        "items go under [items.<id>].")

    plan_due = _date(meta.get("due"), f"{path} [plan]")
    items: dict[str, Item] = {}
    allowed = {"title", "kind", "repo", "topic", "depends_on", "oracle", "brief", "question",
               "due", "estimate", "state", "ref", "base", "engine", "rests_on", "touches",
               "waiting_on", "nag_after", "ask_when"}
    for iid, d in raw_items.items():
        where = f"item {iid!r}"
        if not ID_RE.match(iid):
            raise PlanError(f"{where}: ids are lowercase letters, digits, '.', '_' and '-'.")
        if not isinstance(d, dict):
            raise PlanError(f"{where}: must be a table.")
        extra = set(d) - allowed
        if extra:
            raise PlanError(f"{where}: unknown keys {sorted(extra)}. Known: {sorted(allowed)}.")
        kind = d.get("kind", "")
        if kind not in KINDS:
            raise PlanError(f"{where}: kind {kind!r} is not one of {list(KINDS)}.")
        title = str(d.get("title", "")).strip()
        if not title:
            raise PlanError(f"{where}: needs a title.")
        repo = _path(str(d.get("repo", "")), base)
        if kind == "build" and not repo:
            raise PlanError(f"{where}: a build item needs a repo (an absolute path).")
        oracle = _strs(d.get("oracle"), where, "oracle")
        for spec in oracle:
            try:
                oracles.parse_spec(spec)
            except oracles.OracleError as e:
                raise PlanError(f"{where}: {e}") from None
        if kind != "decision" and not oracle:
            raise PlanError(f"{where}: needs an exit oracle -- 'pr:owner/repo#N', "
                            "'cmd:<shell>', 'file:<path>' or 'manual'.")
        pinned = str(d.get("state", "")).strip()
        if pinned and pinned not in PINNABLE:
            raise PlanError(f"{where}: state {pinned!r} cannot be set in the plan; only "
                            f"{list(PINNABLE)} can. The rest are computed or reported.")
        items[iid] = Item(
            id=iid, title=title, kind=kind, repo=repo,
            topic=str(d.get("topic", "")).strip() or iid,
            depends_on=_strs(d.get("depends_on"), where, "depends_on"),
            oracle=oracle,
            brief=_path(str(d.get("brief", "")), base),
            question=str(d.get("question", "")).strip(),
            due=_date(d.get("due"), where) or plan_due,
            due_own=_date(d.get("due"), where) is not None,
            estimate_days=_days(d.get("estimate"), where),
            pinned=pinned, ref=str(d.get("ref", "")).strip(),
            base=str(d.get("base", "")).strip(),
            engine=str(d.get("engine", "claude")).strip() or "claude",
            rests_on=_strs(d.get("rests_on"), where, "rests_on"),
            touches=tuple(_path(t, base) for t in _strs(d.get("touches"), where, "touches")),
            waiting_on=str(d.get("waiting_on", "")).strip(),
            nag_after_days=_days(d.get("nag_after"), where),
            ask_when=str(d.get("ask_when", "")).strip(),
        )

    mw = meta.get("max_workers")
    if mw is not None and (not isinstance(mw, int) or mw < 1):
        raise PlanError(f"{path} [plan]: max_workers must be a positive integer.")
    pins: dict[str, Pin] = {}
    raw_pins = doc.get("pins", {})
    if not isinstance(raw_pins, dict):
        raise PlanError(f"{path}: [pins] must be a table of [pins.<name>] tables.")
    plan_freeze = _date(meta.get("freeze"), f"{path} [plan]")
    for pname, pd in raw_pins.items():
        where = f"pin {pname!r}"
        if not isinstance(pd, dict) or not str(pd.get("cmd", "")).strip():
            raise PlanError(f"{where}: needs cmd, a shell command that prints the pin's value.")
        extra = set(pd) - {"cmd", "repo", "freeze"}
        if extra:
            raise PlanError(f"{where}: unknown keys {sorted(extra)}; known: cmd, repo, freeze.")
        pins[pname] = Pin(pname, str(pd["cmd"]).strip(), _path(str(pd.get("repo", "")), base),
                          _date(pd.get("freeze"), where) or plan_freeze)
    for it in items.values():
        if it.ask_when:
            if it.kind != "decision":
                raise PlanError(f"item {it.id!r}: ask_when applies only to decision items.")
            parse_ask_when(it.ask_when, items, f"item {it.id!r}")
        for r in it.rests_on:
            if r not in pins:
                raise PlanError(f"item {it.id!r} rests on pin {r!r}, which is not in [pins].")
    rules = str(meta.get("rules", "")).strip()
    plan = Plan(name=name or str(meta.get("name") or base.name), path=path, items=items,
                common_brief=_path(str(meta.get("common_brief", "")), base),
                due=plan_due, max_workers=mw,
                rules=_path(rules, base) if rules else str(base / "rules.md"),
                owns=tuple(_path(o, base) for o in _strs(meta.get("owns"), f"{path} [plan]", "owns")),
                pins=pins)
    validate(plan)
    return plan


ASK_WHEN_WAIT = re.compile(r"^([a-z0-9][a-z0-9._-]*)\s+waits\s*>\s*(\S+)$")


def parse_ask_when(text: str, items: Mapping[str, Item], where: str = "") -> tuple:
    """("date", date) or ("wait", item id, days). Refuses anything else, naming the forms."""
    m = ASK_WHEN_WAIT.match(text.strip())
    if m:
        if m.group(1) not in items:
            raise PlanError(f"{where}: ask_when names {m.group(1)!r}, which is not in the plan.")
        return ("wait", m.group(1), _days(m.group(2), where))
    try:
        return ("date", dt.date.fromisoformat(text.strip()))
    except ValueError:
        raise PlanError(f"{where}: ask_when {text!r} is not '<item> waits > 21d' or a date "
                        "like 2026-11-01.") from None


def validate(plan: Plan) -> None:
    """Refuse dangling dependencies, cycles and two items sharing one worktree."""
    for it in plan.items.values():
        for d in it.depends_on:
            if d not in plan.items:
                raise PlanError(f"item {it.id!r} depends on {d!r}, which is not in the plan.")
            if d == it.id:
                raise PlanError(f"item {it.id!r} depends on itself.")
    cycle = find_cycle(plan.items)
    if cycle:
        raise PlanError("dependency cycle: " + " -> ".join(cycle))
    seen: dict[tuple[str, str], str] = {}
    for it in plan.items.values():
        if it.kind != "build":
            continue
        key = (it.repo.rstrip("/"), it.topic)
        if key in seen:
            raise PlanError(f"items {seen[key]!r} and {it.id!r} both want the worktree "
                            f"{it.repo}/.worktrees/{it.topic}; give one a distinct topic.")
        seen[key] = it.id


def find_cycle(items: Mapping[str, Item]) -> list[str]:
    """One cycle as a closed path of ids, or [] when the graph is acyclic."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {i: WHITE for i in items}
    stack: list[str] = []

    def visit(n: str) -> list[str]:
        colour[n] = GREY
        stack.append(n)
        for d in items[n].depends_on:
            if d not in items:
                continue
            if colour[d] == GREY:
                return stack[stack.index(d):] + [d]
            if colour[d] == WHITE:
                found = visit(d)
                if found:
                    return found
        stack.pop()
        colour[n] = BLACK
        return []

    for n in sorted(items):
        if colour[n] == WHITE:
            found = visit(n)
            if found:
                return found
    return []


def load(path: Path, name: str | None = None) -> Plan:
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise PlanError(f"no plan at {path}.") from None
    return parse(text, path, name)
