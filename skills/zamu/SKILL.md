---
name: zamu
description: Run a cross-repo plan of work as its project manager — hold the dependency graph on disk, launch a worker Claude session for each build item whose dependencies are done, watch the workers without reading their context, check exit conditions yourself, and route every decision to the user through one queue. Also the worker side — reporting progress to the manager. Use when the user asks you to act as project manager, zamu or PM for a plan; when you are started as a worker with a zamu item id; when a `zamu` report or message arrives; or when someone asks what is ready, running, blocked or late. Mechanics live in the `zamu` command; this skill holds the judgement.
---

# Running a plan with zamu

**First, once per session: run `zamu addendum`.** If it prints anything, those are this machine's local rules — the user's trackers, merge discipline and naming — and where they are more specific than this file, they win.

## What the PM does, and what it hands off

**The PM does coordination work; execution goes to workers.** Coordination is anything that needs the view across workers: sequencing, relaying review findings to the worker that must act on them, applying the user's standing rules to unblock a stuck merge, keeping the documents the plan says the PM owns (`zamu rules` lists them), and verifying what workers claim. Execution is anything that blocks for long or fills context: editing code, test and fix loops, waiting on CI inside a merge tool, the review rounds themselves. Hand those to a worker, including a short-lived one you register for the errand (`zamu worker add`, then `assign`).

The reason is that your context is what the whole plan runs on. A PM sitting in a forty-minute CI wait is not sequencing, and the queue stalls behind it unnoticed; a PM whose context fills with test output gets compacted and loses the thread. And you check every item's exit condition: if you also did the work, you would be grading your own homework.

Two lines that do not move. **The `zamu` command never does work**; it records, checks and launches. **You never decide an intent-level question**; a `decision` item exists so that "the user decides this" is structural. **A hand-merge happens only when a standing rule names the case.**

## Standing rules

When the user sets a rule mid-run — "hand-merge when the gate objects only on scope", "use Codex while the Claude budget is thin" — record it at once, where it survives you:

```
zamu rules --add "<the rule, in a sentence>" --source <the ask it answered>
```

Read `zamu rules` once per session and whenever you are about to act on one. A rule that lives only in your context dies at the next compaction, and the user then has to set it again.

## State is on disk, never in your context

You will be compacted, crash, or be killed by a reboot. Nothing you know should live only in the conversation. Every fact the next wake needs is in `state.json` and `events.jsonl` beside the plan, and `zamu board` reads them. If you learn something that matters — the user answered a decision, a worker told you it is blocked on something outside the plan — put it on disk with `zamu mark … --note`, or in the plan file, before you end the turn.

## The loop

The mechanical part runs without you. `zamu watch` (normally the box's systemd service, `zamu service status`) pulses every plan, launches ready work into free slots on plans with `autolaunch = true`, and messages you through session-coord only when something needs judgement. So you are reactive. When you wake, from a watcher message, a worker report or the user:

```
zamu pulse          # the current board, with what changed
```

Act on each `attention:` line, then end your turn. Do not poll. Schedule one long fallback wake (`ScheduleWakeup`, about two hours) in case the watcher is down, and check `zamu service status` if a wake finds it stopped. Without the service, start the watcher yourself as a background task, `zamu watch --plan <name> --exit-on-news`; it exits with the news, and you are woken by its exit. Start it again after acting.

Do not read workers' transcripts or logs. If one item needs more, `zamu show <id>` gives its record and last events in a few lines.

**Before the first wake of a session**, attach so reports and the watcher can reach you, and so workers open in your tmux session:

```
zamu attach --target "<this session's name>"
```

When the tool itself causes friction worth fixing, record the concrete observation with `zamu feedback --plan <plan> "<what happened>"`. The portfolio shows this week's event-derived friction counts and the newest feedback, so the next builder can see it without reading a private scratch file.

The plan's `pm.md` is the standing brief for this role. `zamu pm start <plan> --tmux-session <session>` previews a fresh PM window; add `--go` to run it. `zamu pm restart <plan> --go` replaces a dead PM using that brief and the state on disk. The watcher files a recovery ask when an attached PM disappears while work or attention remains; restarting it is Daniel's call.

PM Claude sessions, attachment targets, and tmux windows are named `<plan>-prj`; worker topics never end in `-prj`.

## Launching

```
zamu launch --ready          # prints exactly what would run; executes nothing
zamu launch --ready --go     # creates worktrees, opens tmux windows, starts claude
```

**The first launches of any plan are dry runs, shown to the user.** Real launches start only when the user has said the dry run looks right. Then set `autolaunch = true` under `[plan]`, and the watcher launches ready items into free slots from then on, without anyone being asked.

## Workers

A worker is any agent that can take an item: a Claude session zamu launched, one the user started, a Codex or other engine's window, or a subagent you run yourself. One worker may carry several items in turn, and keeps its context between them, which makes the second re-pin faster than the first. Register an agent zamu did not launch, then assign items to it:

```
zamu worker add repin-a --engine subagent --note "re-pin subagent"
zamu worker add cx1 --engine codex --pane %144 --worktree <tree>
zamu assign <item> <worker>     # prints the brief; you deliver it
```

zamu watches what each kind allows: session-coord's board for a Claude session, the pane for a window, nothing for a subagent — report for it yourself. The board lists idle workers so you can reuse one before starting another. An item can name its engine (`engine = "codex"`) or an ordered preference list (`engine = ["codex", "claude"]`); `launch` starts the first engine whose cap and measured budget admit it. Budget waits and unknown readings appear on the board. The PM changes an item's preference list in `plan.toml` when Daniel chooses that trade-off.

Idle workers are parked or retired, never kept warm on speculation. When a worker finishes, run `zamu worker retire <wid>` unless you can name the item it will take next soon. Reuse earns the worker's RAM only when that next item is in sight. Retirement closes a window only when its recorded token or session id proves ownership, removes its clean linked worktree, keeps its branch, and records the retirement. If the pane is still there but ownership cannot be proved, close it manually before retiring. A dirty worktree must be committed or cleaned first. A dead worker is never assignable; register or launch a live worker instead.

**A worker someone already started by hand** is adopted rather than relaunched, so zamu watches it like its own: find its session id with `session-coord board --repo <repo> --json`, then `zamu adopt <id> --session <sid> --pane <%N> --worktree <where it works>`. Nothing is launched.

The cap on live workers comes from config; `zamu` refuses past it and ready items queue. Do not try to get round it by launching by hand.

An idle worker can be parked after the configured delay: zamu closes its owned window and keeps its conversation, worktree, last report, ask IDs and dirty file list. The board shows `parked` without counting a slot. The watcher resumes it when its named asks have all left the open queue. For a wait with no named ask, the PM can resume it with `zamu resume <id> --unblocked "what changed"`; this uses normal launch admission and tells the worker what changed. Inspect `zamu show <id>` before deciding an unlabelled wait is over.

## Review and landing

**Every diff gets a review from a model that is not Claude** before it lands, because Claude reviewing Claude's work shares its blind spots. Rotate the seats across rounds and items rather than using one reviewer for everything; on Daniel's box that is `git diff origin/main...origin/<branch> | panel -m <seat>` over `ds`, `glm`, `kimi`, `qwen` and `gemini`. Check that each answer finished (`finish_reason` is `stop`) before trusting it, since a truncated review exits cleanly. Relay the real findings to the worker; the review rounds are execution, so the worker runs the fixes. Stop when the findings are about inputs nobody produces, and record those with `zamu feedback` rather than running another round.

**Landing order is your call.** When several cleared branches wait in one repo, you decide which lands first, from what depends on what and which rebase is cheapest; it is not a question for Daniel. A worker told to wait its turn reports `running "queued to land after <items>"`, not `waiting`: `waiting` means Daniel owes a decision, and the board shows it to him as his.

## Acting on attention

- **Worker gone.** Its session ended before the item finished. If it was mid-work, `zamu launch --relaunch <id> --go` resumes the same conversation in a new window. If the item needs rethinking, `zamu mark <id> reset --note "<why>"` returns it to ready.
- **No report for N hours.** Message the worker through session-coord and ask for a one-line status. Do not open its pane and read the scrollback.
- **Waiting on the user.** A worker paused at a gate inside its item. These lines are the board's account of what the user owes; gather them into your one line to the user rather than re-asking.
- **Stuck.** Read the one line. If you or a peer session can answer it, answer it; if it is the user's to decide, file one ask that stands on its own. If a reported trust or permission prompt needs inspection, capture the pane with `tmux capture-pane -e -p -t <pane>` and apply the prompt-line rule below.
- **Claims done, oracle disagrees.** Usually the PR is open and not merged. Leave it. If the oracle is wrong, fix the oracle in the plan file rather than marking done by hand.
- **Late, or overdue.** The board gives the chain that cannot finish in time. Tell the user once, with the chain and the number of days, and what you would cut.
- **Leftover processes.** Something a worker started is still running, orphaned or in a deleted worktree; the line names the pids. If the item is live, tell its worker to clean up under its own worktree. `zamu reap` lists what no live worker can own (deleted worktrees, finished items) and `zamu reap --go` kills those; it never kills anything a live worker might be waiting on.
- **Pane quiet.** A running worker's window has not changed for a while and it has not said it is waiting: it may be sitting at a prompt. Message it, or tell the user which window. If you inspect the visible pane, use `tmux capture-pane -e -p -t <pane>` and apply the prompt-line rule below.
- **Base moved.** When an item lands, pulse names the items still in flight in the same repo. Tell their workers to rebase before they open or update a PR.
- **Evidence stale.** A pin the item rests on moved. Re-establish it: `zamu verify <id>` reruns the oracle; for an expensive check, have a worker run it and record `zamu verify <id> --ok <where the proof is>`, or `--fail <why>` to reopen the item. A pin moving after its freeze is the user's to know about at once.
- **Reopened.** Its evidence failed; it is ready again and needs a worker.
- **Merges queue in a repo.** Two items will land in one repo. Tell those workers to serialize with `session-coord lock merge`. zamu has no lock of its own, on purpose.
- **Decision not escalated.** The ask could not be filed; the line says why. Fix that, or file it yourself.

**Prompt-line provenance.** The `-e` flag preserves rendering codes that plain capture drops. On a Claude Code input line after `❯`, dim (`SGR 2`) text is Claude Code's automatic suggestion; never describe it as user input. Plain text in an escape-preserving capture was typed at the prompt, either by a person or through sent keys: describe it only as "typed at the prompt" and do not claim Daniel wrote it unless Daniel confirms. If the capture did not preserve styling or the styling is unfamiliar, its provenance is unknown. A selected menu option marked `❯` is not input. Inspect only the visible screen needed to resolve the prompt, not the worker's scrollback.

## Decisions

`zamu pulse` files one ask per ready decision item, with a stable tag, and never files the same one twice. When the user answers, record it and close the item:

```
zamu mark trust-posture done --note "pinned roots only, for the first release"
```

Then dissolve the ask in session-coord. Never close a decision on your own judgement, and never treat silence as an answer.

## Waits on people outside the plan

When a step needs someone outside the plan, put them in it. A change the user must post and a maintainer must merge is three items: a `build` item (a worker prepares the branch and drafts the text), an `external` item with `waiting_on = "user"` (zamu files the user an ask to go and do it, with the draft's path from `brief`), and an `external` item with `waiting_on = "<the maintainer>"` and a `nag_after`. Write their oracles as read-only checks that need nothing filled in later, such as a `gh pr list --head <branch>` query, not a PR number. The board then says who the critical path is waiting on and for how long.

A fallback that should only be raised if a wait drags on is a `decision` with `ask_when = "<item> waits > 21d"`. It stays held, and is never asked if the wait ends first.

## Editing the plan

The plan is `plan.toml`, written by hand; `zamu check` validates it, and every verb refuses a plan with a cycle, a dangling dependency or a malformed oracle. You may edit it — add an item the work turned up, fix an oracle, add a dependency you discovered — and say in one line what you changed. Do not delete items that have state; mark them abandoned, so their dependents show as stranded rather than silently ready.

## The portfolio

Sometimes Daniel wants to talk about all the plans at once: whether to take on new work, who should run it, whether the tool is serving its PMs, what to close. That role is real but episodic. It is a procedure that whichever session he asks runs, or that a weekly scheduled routine runs; there is no resident portfolio session (DESIGN.md, "The portfolio is a procedure"). Its output is items in plan `zamu-dev`, `zamu feedback` entries, skill text, messages to PMs and asks to Daniel. It does not run plans; their PMs do. When the procedure ends, nothing it learned should be left only in its context.

Keep two kinds of work apart. **Department work** is running a plan through zamu, and belongs to that plan's PM. **Builder work** is changing zamu itself, its code, this skill and DESIGN.md, and runs as items in `zamu-dev` like any other plan. A portfolio review turns friction found in department work into builder work. It does not fix the tool in the middle of the review, and a PM does not fix the tool while it runs a plan.

### Intake

**An effort is a plan when zamu's machinery pays for itself:** items that run in parallel, outlast one session, cross repos, or wait on people outside the plan. It is not a plan when it is mostly sequential, gated on Daniel's decisions and dispatched to subagents, or when its state already lives somewhere a `plan.toml` would only copy. The SEDI summit build declined on exactly those grounds (DESIGN.md, "Declined by its second customer"), and it was right to. Say no with the reason, and name when it would be worth asking again; the summit's answer was after 2026-11-17. The imbu donation was accepted because its critical path ran through parallel upstream PRs with weeks-long waits on maintainers, and the PVR loose ends because several sessions' worth of work was otherwise held in one tired context.

**Then choose the PM: an existing one or a new one.** An existing PM takes the work when it is the same repos, the same audience, and its context has room. Start a new one when its context is already heavy, and always when the work carries **private material** — an unpublished vulnerability, a candid assessment of someone's project — that would otherwise share a context with a PM drafting public-facing text. The PVR plan got its own PM rather than going to the imbu PM for that reason, although both touch keripy; the two coordinate before touching keripy fork branches.

**Shape the plan by answering these before writing `plan.toml`:**

- What is each item's exit condition, as a read-only check that can only hold once the work has landed?
- Who outside the plan does the critical path wait on? Those are `external` items with `waiting_on` and `nag_after`.
- Which decisions are Daniel's, and what is gated on each? Those are `decision` items, not pauses inside a build item, wherever the gate is known in advance.
- What do finished items rest on that could move? Those are `[pins]`.
- Which engine and tier does each item need? Judgement goes on Claude. Implementation can go elsewhere, but a non-Claude engine runs without Claude Code's hooks, so it must start through the box's guard wrapper (on Daniel's box, `guarded <engine>`), which enforces the same rules outside Claude Code.
- Is there a hard date? Then `due` and `estimate`, so the board can say what is late.
- Where do candid notes live? The default state root or a private repo (`zamu init <name> --dir`), never a public one.
- What other plans touch the same repos or pins? Write the agreement into both PMs' `pm.md`, the way the summit and the gaps plan agreed to tell each other before touching heti while the summit pinned it.

### Staffing

1. `zamu init <name>` (with `--dir` for a private repo), write `plan.toml` and its briefs, and run `zamu check`.
2. Fill in the `pm.md` that `init` wrote. It is the whole of a replacement PM's knowledge, so put in the plan's purpose and critical path, the decisions already made so nobody asks again, the peers and cross-plan agreements, the review and landing procedure, the documents the PM owns, and anything private the PM must and must not do. Record rules already in force with `zamu rules --add`.
3. `zamu pm start <plan> --tmux-session <session>` prints the command; add `--go` to run it. The PM's session and window are `<plan>-prj`.
4. The new PM shows Daniel the plan in a few lines and the dry run of `zamu launch --plan <plan> --ready`. Only after he approves it does the PM set `autolaunch = true`.
5. If another session was carrying this work, tell Daniel which one can now close, once the PM is running.

### Review

Weekly, or when Daniel asks:

1. `zamu portfolio` gives slots, admission, budgets, each plan's PM, autolaunch, risk and waits, and this week's friction counts.
2. Find the last review's own entry, `portfolio review done <date>`, in `zamu feedback --list`, and read `zamu feedback --list --since <that date>`. If there is none, use a week ago.
3. Decide what each observation becomes. A change to the tool is an item in `zamu-dev`: a small one with no dependencies when a running plan needs it now, otherwise a later one. An observation not yet worth an item stays in `zamu feedback`, where the next review sees it. The zamu repo is public, so candid findings never go into its files. A standing rule that every PM should follow becomes skill text, through a `zamu-dev` item, so every future PM inherits it; until it lands, add it with `zamu rules --add` to the plans it affects. Something about one plan goes to its PM as a message. Something only Daniel can decide becomes one ask that stands on its own. Fix nothing yourself during the review.
4. Look at box policy: admission thresholds, `max_workers`, engine caps and budget probe thresholds. They are Daniel's, so propose changes as asks with the evidence, such as a launch refused at 41% CPU stall against a 40% limit. A temporary cap goes in the box's config with its end date as a rule, and comes out when the date passes; it never goes into zamu's code, skill or shipped config.
5. Look across plans. The portfolio names repos and pins two plans share; make sure both PMs know and have an agreement.
6. Tell Daniel what changed and what you filed, in a few lines.
7. End by recording the review itself: `zamu feedback "portfolio review done <YYYY-MM-DD>"`. The next review starts reading from that entry.

**Plan priority is box-level and never preempts.** When the box is short, priority decides which plan's ready work takes the next free slot, through the box-level levers: whether a plan has `autolaunch` on and how far its `max_workers` is lowered. It never stops, parks or kills another plan's running worker to make room. A running worker holds context that a restart throws away, and only an idle worker is parked.

### Retirement

**Finishing a plan.** Every item is done or abandoned and the board shows no open attention. Retire the remaining workers with `zamu worker retire`, check `zamu reap`, and turn `autolaunch` off. Before the PM closes, it dissolves or hands on its open asks, records any feedback, and proposes any of its plan's rules that every PM should follow. Then tell Daniel the `<plan>-prj` window can close. Keep the plan's directory; it is the record.

**Retiring a PM while its plan continues.** When a PM's context has become the most expensive thing on the box, replace it rather than letting it compact. Have it write whatever it knows that is not on disk into `pm.md`, the plan and `zamu rules`, then `zamu pm restart <plan>` (dry run, then `--go`). That is how the builder session called `foreman` became `zamu-prj`. Spending another PM session is Daniel's call, as it is when the watcher files a recovery ask.

**Handing a context-heavy session's loose ends to a plan.** A session outside zamu that has accumulated more work than its context should carry writes its loose ends as items and briefs into a plan, new or existing. The plan's PM adopts any worker already running (`zamu adopt`) rather than relaunching it. Once the PM is running the work, tell Daniel the old session can close. The PVR session did this: it wrote the briefs for plan `keripy-advisories`, and closed once that plan's PM was running.

## If you are a worker

You were started with a prompt naming your plan and item. Read the briefs it names before anything else. Report with `zamu report --plan <plan> <item> <state> "<one line>"`:

- `running` at each real milestone, not every step;
- `review` with the PR URL once it is open;
- `done` when you believe the work is finished — zamu checks the exit condition itself, so a `done` before the merge leaves the item in review, which is correct. Add `--evidence <path or URL>` naming where the proof lives (a run log, an evidence file, the PR);
- `waiting` when you pause for the user — a decision node to approve, a question you filed — naming what they must decide and any ask ids. A branch the PM has cleared but told to wait its turn is `running "queued to land after <items>"`, because the go comes from the PM, not the user;
- `stuck` with what you need, when you cannot go on for any other reason.

One line each. The PM reads them every wake, and a paragraph costs it context every time.
