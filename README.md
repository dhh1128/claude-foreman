# zamu

*Swahili: a turn of duty; a watch. Called `foreman` until 2026-10-12; the `foreman` command still works for one release.*

A Claude Code plugin that acts as the project manager for a plan of work spread across several repositories. It holds the dependency graph on disk, launches a worker Claude session for each item whose dependencies are done, watches the workers without reading their context, checks each item's exit condition itself, and routes every decision to you through one queue. It never does the work: it does not edit code, merge, or post anywhere.

It is a sibling of [session-coord](https://github.com/dhh1128/claude-session-coord), which keeps sessions in one project from tangling, and depends on it for liveness, the ask queue and messaging.

## What problem it solves

When a change has to land in one repo before the next can take it, and another waits on an upstream release, and a third needs a decision only you can make, somebody has to hold that graph. Without a tool that somebody is you: you notice the release, open the next window, paste the next brief, and remember who is waiting on what. zamu holds it instead.

## Install

```
/plugin marketplace add dhh1128/claude-zamu
/plugin install zamu@zamu
```

Install session-coord the same way. Requirements: Python 3.11 or later, git, tmux, and `gh` for pull-request oracles.

## A plan

```
zamu init roadmap                     # ~/.local/state/zamu/plans/roadmap/plan.toml
zamu init roadmap --dir ~/private/plans/roadmap   # kept in a private repo, linked by name
```

`init` also writes `pm.md` beside `plan.toml`. Fill in that standing PM brief with the plan's context, peers and rules so a replacement session can take over from disk.

```toml
[plan]
due = 2026-11-17
common_brief = "briefs/common.md"

[items.lib-release]
title = "lib 0.10.0 released"
kind = "external"                 # waits on the world; zamu only polls
oracle = "cmd:pip index versions lib | grep -q 0.10.0"

[items.app-bump]
title = "Bump lib in app"
kind = "build"                    # a worker session does it
repo = "/home/me/code/app"
depends_on = ["lib-release"]
oracle = "pr:me/app#42"           # done when merged; zamu checks, not the worker
brief = "briefs/app-bump.md"
estimate = "2d"

[items.posture]
title = "Release posture"
kind = "decision"                 # you decide; zamu files one ask and never launches
question = "Ship the app bump in 1.4 or hold it for 2.0?"
```

A plan may also declare pins its evidence rests on, and the documents the PM itself maintains:

```toml
[plan]
freeze = 2026-10-16
owns = ["/home/me/code/app/docs/status.md"]

[pins.core]
cmd = "git -C /home/me/code/app show HEAD:pyproject.toml | grep -o 'core.git@[0-9a-f]*'"

[items.release-check]
rests_on = ["core"]               # a moved pin makes this item's evidence stale
engine = "codex"                  # launched with config's [engines] codex command
# engine = ["codex", "claude"]     # try in order; a single string keeps its old meaning
tier = "standard"                 # deep, standard, or light; mapped by box config per engine
# model = "gpt-5.3-codex"         # optional exact model, overriding the tier mapping
```

Items can also say who they wait on and when to nag, and a decision can be held until a wait drags on:

```toml
[items.upstream-merges]
kind = "external"
waiting_on = "Sam Smith"          # "user" means you; zamu then files you an ask to do it
nag_after = "14d"

[items.fork-fallback]
kind = "decision"
ask_when = "upstream-merges waits > 21d"   # or a date
```

Oracles are `pr:owner/repo#N` (merged), `cmd:<shell>` (exits 0), `file:<path>` (exists), or `manual` (closed with `zamu mark`). A list means all must hold. Cycles, dangling dependencies and malformed oracles are refused when the plan loads.

## Commands

```
zamu board                  the compact status a waking PM reads
zamu portfolio [--json]     one compact board across every plan on this box
zamu feedback --plan <p> "<text>"   record a field observation
zamu feedback --list [--since YYYY-MM-DD]   list observations
zamu priority [<plan> <n> | --reserve <plan> <slots>]  show or set box scheduling order
zamu pulse                  check oracles and workers, escalate decisions, print the board
zamu launch --ready         what would launch now; prints the exact commands, runs nothing
zamu launch --ready --go    create worktrees, open tmux windows, start workers
zamu launch --relaunch <id> --go   resume a dead worker's conversation
zamu resume <id>              resume a parked worker when its named asks close
zamu resume <id> --unblocked "why"   PM clears a wait with no named ask
zamu adopt <id> --session <sid>   take on a worker started by hand
zamu worker add <wid> --engine codex|subagent|claude [--pane] [--session]
zamu worker list              show registered workers and their last checked status
zamu worker retire <wid>      close an idle worker's proven window and clean worktree; keep its branch
zamu assign <id> <wid>      give an item to a registered worker; prints its brief
zamu verify <id> [--ok EVIDENCE | --fail WHY]   after a pin moved
zamu rules [--add TEXT --source ASK]   the plan's standing rules
zamu report <id> <running|review|done|stuck> "<line>"   for workers
zamu mark <id> <done|abandoned|reset> --note "<why>"
zamu attach --target <pm>   let worker reports wake the PM session
zamu pm start <plan> --tmux-session <session>       preview a fresh PM window
zamu pm start <plan> --tmux-session <session> --go  start it
zamu pm restart <plan>       preview replacement of a dead PM
zamu pm restart <plan> --go  start the replacement
zamu reap [--go]            list leftover processes; kill those no live worker owns
zamu watch [--plan] [--exit-on-news]   pulse, autolaunch, wake PMs only with news
zamu service install|status|uninstall  run the watcher as a systemd user service
zamu budget codex|openrouter|claude    standalone account budget probes
zamu show <id>              one item in full
zamu check | plans | addendum
```

PM commands use the tmux session and repo recorded by `attach`; `--tmux-session` overrides the session and is required when the plan has no prior attachment. On a plan's first start, run the command from the repo the PM should use. Both commands default to a dry run. The watcher files an `A` ask to restart a PM when its session has disappeared and the plan has active workers or attention; it never starts one automatically.

## Where it keeps things

`~/.local/state/zamu/plans/<name>/`: `plan.toml` and `pm.md` (yours), `state.json` and `events.jsonl` (zamu's). Override the root with `ZAMU_HOME`. Workers' worktrees are `<repo>/.worktrees/<topic>`.

A worker confined to its worktree can append a newline terminated report to `<worktree>/.zamu/reports`: `<ISO time> <running|review|done|stuck|waiting> <one line>`. Each pulse reads complete new lines from active workers and handles them like `zamu report`. zamu creates `.zamu/.gitignore` when it launches a worktree.

## Configure

`~/.config/zamu/config.toml`, every key optional:

```toml
max_workers = 3            # ZAMU_MAX_WORKERS overrides; a plan can only lower it
park_after_minutes = 30   # idle workers give back their windows and slots
stall_hours = 4
launch_grace_minutes = 3
tmux_session = ""          # default: the session zamu is run from
claude = ["claude"]        # the command that starts a worker, with any flags it needs
escalate = "session-coord" # or "none"

[priority]
roadmap = 5              # higher goes first; unlisted plans default to 0

[reserve]
roadmap = 1              # desired minimum slots while it has ready work
```

Workers are started by tmux, not by your interactive shell, so a shell function or alias that adds flags to `claude` does not apply to them. Put the flags in `claude` (or under `[engines]`). Choose them knowingly: a worker runs unattended, so a permission prompt stalls it where nobody sees, while a flag that skips permissions or a sandbox lets it act unreviewed. zamu defaults to neither.

A worker that reports `waiting` or `stuck`, or whose pane and process tree stay idle, is parked after `park_after_minutes`. Its window closes, but its session, worktree, dirty file list, last report and ask IDs remain in state. Parked items use no slot. Under autolaunch, the watcher resumes a parked worker when named asks it saw open at parking have all left the queue; for an unlabelled or unfiled wait, the PM can run `zamu resume <id> --unblocked "what changed"`. Resume uses the worker's saved conversation and the usual slot, admission and engine budget checks.

The all-plan watcher pulses every plan before allocating free slots. It serves plans below their reserves first, then higher priorities, breaking ties by the oldest ready item. Priority never stops a running worker. Use `zamu priority` to inspect the order, `zamu priority <plan> <n>` to set a priority, or `zamu priority --reserve <plan> <slots>` to set a reserve. Changes are logged with the actor in `~/.config/zamu/priority.jsonl`.

Engine budgets can be measured with optional commands in `~/.config/zamu/config.toml`:

```toml
budget_refresh_minutes = 10
budget_ask_minutes = 60

[engines]
codex = ["codex"]

[models.claude]
deep = "fable"
standard = "opus"
light = "sonnet"

[models.codex]
deep = { model = "gpt-5.3-codex", reasoning_effort = "high" }
standard = { model = "gpt-5.3-codex", reasoning_effort = "medium" }
light = { model = "gpt-5.3-codex", reasoning_effort = "low" }

[budgets.codex]
probe = ["zamu", "budget", "codex", "--max-pct", "60"]

[budgets.claude]
probe = ["zamu", "budget", "claude", "--projects", "~/.claude/projects", "--max-pct", "85"]
```

An item with no `tier` or `model` uses its engine's default model. A tier requires a mapping for the engine chosen at launch; a missing mapping stops that launch with a configuration error. An explicit `model` on the item takes precedence over its tier and uses the engine's default reasoning effort. `zamu launch` shows the tier and resolved model in its dry run, and `zamu show` shows the planned or launched model. Claude receives `--model`; Codex receives `-m` and, when configured, `-c model_reasoning_effort=...`. Budgets remain keyed to the engine account, and the chosen model is saved on the launch record.

A probe prints one JSON object with optional `figure`, `used_pct`, `resets_at`, and `estimate` fields. Exit 0 admits, 75 means not now, and any other exit means zamu could not look. A missing budget entry always admits that engine through this check. An unknown reading can be used only if no preferred engine admits; the board shows it as unknown. CPU and memory admission still falls back to the count cap when its check cannot look. The difference is deliberate: a budget failure can consume a whole worker session, while a box check failure is bounded by the worker count.

`zamu budget codex` reads Codex rollout limits. `zamu budget openrouter --min-usd 5` reads the OpenRouter key via `llm keys get openrouter` and checks remaining credit. `zamu budget claude` estimates usage from one account's transcript directory; run `zamu budget claude --projects ~/.claude/projects calibrate 76` after reading 76% on Claude's usage page to set its allowance. For a known weekly reset, pass `--reset "Thu 17:00 UTC"`; without it the estimate uses a rolling seven day window. `~/.config/budget/claude.toml` can set `[model_factors]` for `opus`, `sonnet`, and `haiku`; the defaults are 5, 3, and 1. The Claude figure is an estimate and needs recalibration if its model mix changes.

`zamu portfolio` shows machine slots and engine caps, admission and budget readings, watcher health and each plan's last tick. Plan lines are sorted by live workers and show process tree RSS, sampled CPU%, estimated session usage by engine, PM status, autolaunch, nearest deadline risk and user waits. Claude spend is in weighted tokens (`wtok`), and Codex spend is in raw tokens (`tok`); `?` means the measurement could not be made, `~` marks an estimate, and `+?` means some worker logs could not be read alongside a measured amount. The board ranks open asks and waiting workers by unfinished downstream items, then names active repo and pin conflicts across plans. `--json` provides the same facts for tools.

The portfolio also shows friction this week with changes from the previous UTC Monday to Sunday week: adopted hand-started workers, workers seen live after being gone, admission and budget holds with elapsed hours, waits crossing `nag_after`, reports filed from the PM pane, and verify reopens. It shows the three newest feedback observations. Feedback lives in `~/.local/state/zamu/feedback.jsonl`; the first feedback or portfolio read imports dated bullets from the old field notes once per bullet.

House rules for the skill go in `~/.config/zamu/addendum.md` (or `ZAMU_ADDENDUM`), which the skill reads first and treats as more specific than itself.

## Development

```
uv run --no-project --python 3.11 --with pytest python -m pytest tests -q
```

`tests/test_acceptance.py` drives the real command against temp git repos and fake session-coord, tmux and gh. `skills/zamu/DESIGN.md` records the decisions and what has not been verified.
