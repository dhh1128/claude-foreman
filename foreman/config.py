"""Where things live, and the few settings a machine or a plan can change.

State root: `$FOREMAN_HOME`, default `~/.local/state/foreman`. Each plan is a directory under
`plans/<name>/` holding `plan.toml`, `state.json`, `events.jsonl` and any briefs. That entry may
be a symlink to a directory elsewhere -- a private git repo, for history -- which is how a plan
lives outside every repo it manages without the CLI needing two code paths.

Config: `$FOREMAN_CONFIG`, default `~/.config/foreman/config.toml`. Every key is optional:

    max_workers = 3          # the machine's cap on live workers
    stall_hours = 4          # no report for this long flags a worker as stalled
    launch_grace_minutes = 3 # a worker this new is not yet expected on the board
    tmux_session = ""        # where worker windows open; default the caller's tmux session
    claude = ["claude"]      # the command that starts a worker, before its own flags
    [engines]                # other engines an item can name with `engine = "<name>"`
    codex = ["codex", "--dangerously-bypass-approvals-and-sandbox"]
    escalate = "session-coord"   # or "none": list decisions on the board, file nothing
    idle_minutes = 30        # a leftover process whose CPU has not moved this long is "idle"
    pane_quiet_minutes = 30  # a running worker's pane unchanged this long may be at a prompt

`FOREMAN_MAX_WORKERS` overrides `max_workers`. A plan's own `max_workers` can only lower the
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


def home() -> Path:
    return Path(os.environ.get("FOREMAN_HOME") or "~/.local/state/foreman").expanduser()


def plans_dir() -> Path:
    return home() / "plans"


def config_path() -> Path:
    return Path(os.environ.get("FOREMAN_CONFIG") or "~/.config/foreman/config.toml").expanduser()


def addendum_path() -> Path:
    return Path(os.environ.get("FOREMAN_ADDENDUM") or "~/.config/foreman/addendum.md").expanduser()


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
    env = os.environ.get("FOREMAN_MAX_WORKERS")
    if env:
        kw["max_workers"] = env
    try:
        mw = int(kw.get("max_workers", DEFAULT_MAX_WORKERS))
    except (TypeError, ValueError):
        raise ConfigError(f"max_workers must be an integer, not {kw.get('max_workers')!r}.") from None
    if mw < 1:
        raise ConfigError("max_workers must be at least 1.")
    kw["max_workers"] = mw
    engines = kw.get("engines", {})
    if not isinstance(engines, dict):
        raise ConfigError("[engines] must map an engine name to a command list.")
    engines = {k: tuple([v] if isinstance(v, str) else v) for k, v in engines.items()}
    engines.setdefault("claude", tuple(kw.get("claude", ("claude",))))
    kw["engines"] = engines
    if kw.get("escalate", "session-coord") not in ("session-coord", "none"):
        raise ConfigError("escalate must be \"session-coord\" or \"none\".")
    return Config(**kw)


def resolve_plan_dir(ref: str | None) -> Path:
    """A plan name, a directory, or a plan.toml path; else $FOREMAN_PLAN; else the only plan."""
    ref = ref or os.environ.get("FOREMAN_PLAN") or ""
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
        raise ConfigError("no plans yet; create one with `foreman init <name>`.")
    raise ConfigError(f"several plans exist ({', '.join(known)}); pass --plan <name>.")


def known_plans() -> list[str]:
    d = plans_dir()
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if (p / "plan.toml").exists())
