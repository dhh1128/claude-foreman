# foreman — design

`SKILL.md` holds judgement for the session doing the work. This holds the decisions and why, for whoever comes next.

## The problem

session-coord keeps sessions in one project from tangling. It has no notion of a plan, and it does not cross repos. The user it was built for runs eight to fifteen Claude sessions at once, and a program of work like a library release rippling through four downstream repos has dependencies that cross repos: a dependency bump that must land in one repo before the next can take it, changes that wait on upstream pull requests only the human may post, decisions that must be recorded before the code they justify.

Before this, the human held that graph. He was the bottleneck and the switchboard: he noticed when an upstream release landed, opened the next window, pasted the next brief, and remembered which repos were waiting on what. foreman holds the graph, launches worker sessions when an item's dependencies are met, watches them, and escalates. It does nothing else.

This was the third time the need came up, which is why it was built generically. One project had already built a bespoke PM as a long prompt (a session told, in prose, to own an agenda file, order five work items, cap workers at four, and tell the human when to launch the next handoff). A second has a hard date — a summit in November — and so needs "what is late", which the first never exercised.

## Decisions, with the reasoning

**The plan and the record are two files.** `plan.toml` is the human's: what the work is and what depends on what. `state.json` is the CLI's: what has happened. Neither writes the other. A person can edit the plan while the PM runs, and the next pulse picks the edit up without either side overwriting the other. The alternative — one file with state fields the CLI rewrites — makes every hand edit a race with the PM.

**Nothing lives in the PM's context.** Every verb reads disk, acts, and writes what it learned before returning. There is no daemon and no in-memory cache, so there is nothing for a crash, a compaction or a reboot to lose. The acceptance test kills a pulse with SIGKILL partway through and checks that a fresh process sees byte-identical state.

**Writes are atomic and locked; unreadable state is refused, never treated as empty.** All three are session-coord's rules, each learned there by losing data. A tolerant reader that returns `{}` for a file it could not parse hands the next write a licence to save `{}` over it.

**An oracle answers True, False or None, and None means could not look.** A `gh` outage read as "not merged" would only delay. Read as merged, it would launch dependents onto a base without the change. So None is never folded into either answer. Same rule as session-coord's liveness, for the same reason.

**A worker's "done" is a claim, checked against the oracle.** A done that the oracle does not confirm holds the item in `review`, flagged. The cost of a wrong done is a dependent launched onto the wrong base, discovered when that dependent's work is half built.

**Liveness is session-coord's, through its command.** That logic took session-coord eleven review rounds: pid recycling, tmux server restarts, panes holding a different claude from the one recorded. A second copy would drift from the first the day either changed. foreman starts each worker with `claude --session-id <uuid>` chosen at launch, so it knows the id before the worker says anything, and a worker is live iff `session-coord board --repo <repo> --json` lists that id. When session-coord cannot be read, the worker is `unknown`, which holds its slot. A dead worker does not. Calling the command rather than importing the package is deliberate: the two plugins are installed and upgraded separately.

**Relaunch resumes the conversation.** `launch --relaunch` uses `claude --resume <the same uuid>` in a new window over the existing worktree, so a worker that died mid-task comes back with its own context. It refuses unless the board says the worker is dead, because a relaunch of a live worker is two workers on one branch.

**Escalation is idempotent by construction.** A decision's ask tag is a hash of the plan name and item id, and the tag is recorded in state once filed. A restart that lost state refiles the same tag, session-coord refuses the duplicate, and foreman records that as already filed. The acceptance test deletes `state.json` after an escalation and checks that the queue still holds one ask.

**`ask add` is checked by its confirmation, not its exit status.** Outside a git repo session-coord prints "not a git repo" and exits 0 having filed nothing. Trusting the exit status would record an escalation that never happened.

**The machine's cap wins.** `max_workers` comes from config (`FOREMAN_MAX_WORKERS` overrides it) and defaults to 3. A plan may set its own, which can lower the cap but never raise it: the machine's limit exists because of memory, and a plan written on another machine does not know this one's.

**Launches are data before they are actions.** `launch` builds the exact command list and either prints it or runs it; the dry run is the same list, not a description. The first launches of a plan are dry runs shown to the human, which is a rule in the skill rather than in the code, because after that one review launching is the PM's job.

**Merge contention is shown, not managed.** Two build items ready or in flight in one repo put a line on the board pointing at `session-coord lock merge`. A second lock would have two owners for one invariant.

**Deadlines are a check, not a scheduler.** Items carry `due` (inherited from `[plan] due` when absent) and an optional `estimate`. An item is at risk when the longest chain of unfinished estimates ending at it cannot finish by its due date starting today. That answers "what is late" without pretending to know who works on what, or when. Only a date written on a dependency counts when flagging a dependency due after its dependent; otherwise a plan-wide date inherited by everything flags every item that has an earlier date of its own, which the first version did.

**TOML, so Python 3.11.** Daniel's call, 2026-10-12. The first proposal was INI via `configparser`, to keep session-coord's 3.9 floor for a stock Mac. He chose TOML and 3.11, and `bin/foreman` says so plainly on an older interpreter rather than failing on the import.

**The board is a budget.** A waking PM reads it every time. Attention first, one line per worker in flight, everything merely waiting compressed to ids. A test holds a twenty-item plan's board under 1600 characters, about 400 tokens.

## What the first real plan changed (2026-10-12)

The first PM to run a real plan (five gap items in one repo, one worker already running by hand) reported in its first hour; each item below is its observation, with the cause re-derived here.

**Launched workers were invisible to session-coord.** The PM saw that workers started by `launch` never appeared on `session-coord board`, while a hand-started one did. Cause: `launch` ran claude as the tmux pane's own process, and session-coord looks for a pane's claude only among the pane process's *descendants*, so the record was swept as stale on the next read. Two fixes. `launch` now runs claude as the child of a shell (`sh -c '"$@"; exec $SHELL -l'`), which is the shape of every hand-started window; the trailing `exec` leaves a shell in the worktree when claude exits, as a hand-started window does. And liveness gained a second proof: when session-coord does not list the session, the recorded pane is checked for a process whose argv carries the session id. A worker foreman started always has its id in its argv, so this proves exactly the workers foreman launched, and says nothing about anyone else's.

**Flags did not reach the workers.** Daniel's sessions all run with a permission-mode flag that comes from a shell function, and tmux does not run an interactive shell, so the flag never reached a launched worker, which would have sat at permission prompts in a window nobody watched. The `claude` config key already carried flags; the README now says why it has to. A handoff telling the worker to ask the human before starting was the same failure by another route, so the worker prompt now says the window is unattended and to report `stuck` instead of waiting.

**`adopt` was missing**, because the plan's first worker was started by hand before foreman existed. Added, with a warning when the recorded tree does not exist yet.

**The PM's own report rang the PM.** `attach` records the PM's pane, and a report made from it no longer rings it.

**Merge contention fired from the first launch**, before any PR existed, on every pulse and with no way to acknowledge it. It now fires only when two items in one repo are both in review, which is when merges actually queue.

## What was harvested from the bespoke PM

The bespoke PM was a prompt kept in a private repository. foreman took from it: a brief shared by every worker (`common_brief`), a free-text `ref` field for the item's tracker id, a worker cap, and the conductor-and-ask discipline. It differs on one point that mattered: the prompt PM told the human to launch the next worker when its dependency merged. Launching is the thing foreman exists to take off him.

It also modelled a decision *inside* a work item ("G14 stops once for approval of a decision node"). foreman models decisions as items of their own, so the dependency is visible on the board. A worker that hits an unplanned decision reports `stuck`, and the PM files the ask.

## Declined by its second customer

The summit build, with a hard date five weeks out, was offered foreman on 2026-10-12 and its conductor declined, for reasons worth keeping. What remained was a few sequential items gated on the human's decisions. Its state already lived in a narrative plan, ticks and the session-coord ask queue, so a plan file would have been a second copy. And it dispatched work as resumable Agent subagents, not tmux sessions, so foreman's launcher would have been a second dispatch mechanism. foreman's value is concentrated where items run in parallel and outlast one session. A plan that is mostly sequential, gated on decisions, and run by subagents gets little from it, and switching mid-stream costs more than it saves. foreman cannot launch or watch a subagent at all. That is a gap if subagent dispatch turns out to be common.

## Rejected

- **INI** (configparser, Python 3.9). Proposed; Daniel chose TOML.
- **A daemon or an MCP server.** It would need a process kept alive across reboots, which is the very failure the disk-only state exists to survive.
- **The Workflow tool.** Workflows run subagents, not sessions, and need separate opt-in.
- **Reading worker transcripts to judge progress.** Expensive on every wake and unnecessary: workers report one line, oracles say what is done, session-coord says who is alive.
- **Trusting a worker's "done".** See above.
- **Its own merge lock, its own liveness, its own message channel.** session-coord has all three.

## Known gaps — what has not been verified

Reported by the first real PM and not yet built:

- **Decision gates inside a build item** are now reported as `waiting`, which puts an attention line on the board (the PM had seen gated workers report `running "...awaiting Daniel"`, which the board did not flag). The ask itself is still filed by the worker, not by foreman.
- **The PM is not registered where its workers are.** Claiming the conductor role in the work repo needed a hand `session-coord register` there. `attach` could do it.
- **`foreman` is not on PATH in a session that predates the plugin install**, and the versioned cache path changes on every update. A stable shim outside the plugin would fix it; none is installed by the plugin.

- **A real launch has never run.** The acceptance tests drive real git (worktrees created from a bare origin) but fake tmux, claude, gh and session-coord. Untested: that `tmux new-window … claude --session-id <uuid> "<prompt>"` starts a worker that takes the prompt as its first turn; that session-coord's SessionStart hook records the same id that was passed to `--session-id`; and that tmux-claude's `save`/`restore` round-trips a worker started this way.
- **The PM's ask repo.** Asks go to the project recorded by `foreman attach`, else the directory `pulse` runs in. If that is not a git repo, session-coord files nothing, and the board says the escalation failed. Not tried against a real queue.
- **Closing a decision is manual** (`foreman mark <id> done`). The PM has to notice the answer; pulse does not read the ask queue, because session-coord has no machine-readable `ask list`.
- **No `hold` or `defer`.** A decision the human has deliberately deferred is still escalated once. The plan can only express it by leaving the item out, or by marking it abandoned.
- **An oracle that holds closes its item even while the item's dependencies are open.** That is right for an external event that happened early, and surprising for a build item: a `cmd:` oracle that happens to exit 0 closes the item without a worker. Write build oracles that can only hold once the work is merged.
- **Stall is measured from the last report**, not from activity in the worktree. A worker that works for five hours without reporting is flagged.
- **Deadline arithmetic is calendar days**, with no notion of working days or of parallel hands.
- **Python 3.11 is assumed, not tested on other versions.** The suite runs on 3.11.
