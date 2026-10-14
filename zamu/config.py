"""Where things live, and the few settings a machine or a plan can change.

State root: `$ZAMU_HOME`, default `~/.local/state/zamu`. Each plan is a directory under
`plans/<name>/` holding `plan.toml`, `state.json`, `events.jsonl` and any briefs. That entry may
be a symlink to a directory elsewhere -- a private git repo, for history -- which is how a plan
lives outside every repo it manages without the CLI needing two code paths.

Config: `$ZAMU_CONFIG`, default `~/.config/zamu/config.toml`. Every key is optional:

    max_workers = 3          # the machine's cap on live workers
    stall_hours = 4          # no report for this long flags a worker as stalled
    launch_grace_minutes = 3 # a worker this new is not yet expected on the board
    tmux_session = ""        # where worker windows open; default the caller's tmux session
    claude = ["claude"]      # the command that starts a worker, before its own flags
    [engines]                # other engines an item can name with `engine = "<name>"`
    codex = ["codex", "--sandbox", "workspace-write"]

Workers run unattended, so whatever permission mode their command sets is the mode they run
in with nobody watching. Bypassing an engine's sandbox or approvals is the user's decision
for their own machine; nothing here defaults to it.
    escalate = "session-coord"   # or "none": list decisions on the board, file nothing
    admission = ["headroom", "check"]   # measured admission; max_workers stays a backstop
    idle_minutes = 30        # a leftover process whose CPU has not moved this long is "idle"
    pane_quiet_minutes = 30  # a running worker's pane unchanged this long may be at a prompt

`ZAMU_MAX_WORKERS` overrides `max_workers`. A plan's own `max_workers` can only lower the
cap, never raise it: the machine's limit exists because of memory, and a plan file written on
another machine does not know this one's.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MAX_WORKERS = 3


class ConfigError(ValueError):
    pass


def env(name: str) -> str:
    """ZAMU_<name>, else the FOREMAN_<name> it was called before the rename (2026-10-12)."""
    return os.environ.get(f"ZAMU_{name}") or os.environ.get(f"FOREMAN_{name}") or ""


def home() -> Path:
    return Path(env("HOME") or "~/.local/state/zamu").expanduser()


def plans_dir() -> Path:
    return home() / "plans"


def config_path() -> Path:
    return Path(env("CONFIG") or "~/.config/zamu/config.toml").expanduser()


def addendum_path() -> Path:
    return Path(env("ADDENDUM") or "~/.config/zamu/addendum.md").expanduser()


@dataclass(frozen=True)
class Config:
    max_workers: int = DEFAULT_MAX_WORKERS
    stall_hours: float = 4.0
    launch_grace_minutes: float = 3.0
    tmux_session: str = ""
    claude: tuple[str, ...] = ("claude",)
    #: engine name -> command. `claude` is filled from the `claude` key when absent.
    engines: dict = field(default_factory=dict)
    escalate: str = "session-coord"
    idle_minutes: float = 30.0       # CPU still this long marks a leftover process as idle
    pane_quiet_minutes: float = 30.0 # a running worker's pane unchanged this long: at a prompt?
    #: A command asked before each launch: exit 0 admit, 75 not now, anything else could not
    #: look (the count cap alone then applies). Empty: the count cap alone.
    admission: tuple[str, ...] = ()
    #: engine name -> most live workers of that engine across all plans, e.g. {"claude": 2}
    engine_caps: dict = field(default_factory=dict)
    #: engine -> command returning JSON and exit 0 (admit), 75 (not now), other (unknown).
    budgets: dict = field(default_factory=dict)
    budget_refresh_minutes: float = 10.0
    budget_ask_minutes: float = 60.0

    def __post_init__(self):
        if "claude" not in self.engines:
            object.__setattr__(self, "engines", {**self.engines, "claude": self.claude})

    def cap(self, plan_cap: int | None) -> int:
        return min(self.max_workers, plan_cap) if plan_cap else self.max_workers


def load() -> Config:
    p = config_path()
    try:
        doc = tomllib.loads(p.read_text())
    except FileNotFoundError:
        doc = {}
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{p}: not valid TOML: {e}") from None
    known = set(Config.__dataclass_fields__)
    extra = set(doc) - known
    if extra:
        raise ConfigError(f"{p}: unknown keys {sorted(extra)}; known: {sorted(known)}.")
    kw = dict(doc)
    if "claude" in kw:
        if isinstance(kw["claude"], str):
            kw["claude"] = (kw["claude"],)
        kw["claude"] = tuple(kw["claude"])
    env_cap = env("MAX_WORKERS")
    if env_cap:
        kw["max_workers"] = env_cap
    try:
        mw = int(kw.get("max_workers", DEFAULT_MAX_WORKERS))
    except (TypeError, ValueError):
        raise ConfigError(f"max_workers must be an integer, not {kw.get('max_workers')!r}.") from None
    if mw < 1:
        raise ConfigError("max_workers must be at least 1.")
    kw["max_workers"] = mw
    if "admission" in kw:
        a = kw["admission"]
        import shlex
        kw["admission"] = tuple(shlex.split(a) if isinstance(a, str) else a)
    caps = kw.get("engine_caps", {})
    if not isinstance(caps, dict) or not all(isinstance(v, int) and v >= 0 for v in caps.values()):
        raise ConfigError("[engine_caps] maps an engine name to a non-negative integer.")
    engines = kw.get("engines", {})
    if not isinstance(engines, dict):
        raise ConfigError("[engines] must map an engine name to a command list.")
    engines = {k: tuple([v] if isinstance(v, str) else v) for k, v in engines.items()}
    engines.setdefault("claude", tuple(kw.get("claude", ("claude",))))
    kw["engines"] = engines
    budgets = kw.get("budgets", {})
    if not isinstance(budgets, dict):
        raise ConfigError("[budgets] must map engine names to probe commands.")
    parsed = {}
    for name, spec in budgets.items():
        if not isinstance(spec, dict) or set(spec) != {"probe"}:
            raise ConfigError(f"[budgets.{name}] needs only a probe command.")
        command = spec["probe"]
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise ConfigError(f"[budgets.{name}].probe must be a nonempty command list.")
        parsed[name] = tuple(command)
    kw["budgets"] = parsed
    for key in ("budget_refresh_minutes", "budget_ask_minutes"):
        if not isinstance(kw.get(key, 10), (int, float)) or kw.get(key, 10) <= 0:
            raise ConfigError(f"{key} must be positive.")
    if kw.get("escalate", "session-coord") not in ("session-coord", "none"):
        raise ConfigError("escalate must be \"session-coord\" or \"none\".")
    return Config(**kw)


def resolve_plan_dir(ref: str | None) -> Path:
    """A plan name, a directory, or a plan.toml path; else $ZAMU_PLAN; else the only plan."""
    ref = ref or env("PLAN") or ""
    if ref:
        p = Path(ref).expanduser()
        if p.name == "plan.toml":
            return p.parent
        if p.is_dir() and (p / "plan.toml").exists():
            return p
        named = plans_dir() / ref
        if (named / "plan.toml").exists():
            return named
        raise ConfigError(f"no plan named {ref!r}. Known: {', '.join(known_plans()) or 'none'}.")
    known = known_plans()
    if len(known) == 1:
        return plans_dir() / known[0]
    if not known:
        raise ConfigError("no plans yet; create one with `zamu init <name>`.")
    raise ConfigError(f"several plans exist ({', '.join(known)}); pass --plan <name>.")


def known_plans() -> list[str]:
    d = plans_dir()
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if (p / "plan.toml").exists())
