---
name: foreman
description: Run a cross-repo plan of work as its project manager — hold the dependency graph on disk, launch a worker Claude session for each build item whose dependencies are done, watch the workers without reading their context, check exit conditions yourself, and route every decision to the user through one queue. Also the worker side — reporting progress to the manager. Use when the user asks you to act as project manager, foreman or PM for a plan; when you are started as a worker with a foreman item id; when a `foreman` report or message arrives; or when someone asks what is ready, running, blocked or late. Mechanics live in the `foreman` command; this skill holds the judgement.
---

# Running a plan with foreman

**First, once per session: run `foreman addendum`.** If it prints anything, those are this machine's local rules — the user's trackers, merge discipline and naming — and where they are more specific than this file, they win.

## What the PM is, and what it is not

The PM holds the plan, starts workers when an item's dependencies are done, watches them, and escalates. **It does nothing else.** It never edits code, never merges, never posts to GitHub or anywhere else, and never decides an intent-level question. Workers own their branches and pull requests. A `decision` item exists so that "the user decides this" is a structural fact in the plan rather than a habit the PM has to remember.

The reason: a PM that starts doing the work stops sequencing, and nobody notices the queue stall behind it.

## State is on disk, never in your context

You will be compacted, crash, or be killed by a reboot. Nothing you know should live only in the conversation. Every fact the next wake needs is in `state.json` and `events.jsonl` beside the plan, and `foreman board` reads them. If you learn something that matters — the user answered a decision, a worker told you it is blocked on something outside the plan — put it on disk with `foreman mark … --note`, or in the plan file, before you end the turn.

## The loop

Each wake, whether from a worker's message, a timer, or the user:

```
foreman pulse          # checks oracles and workers, files decision asks, prints the board
```

Read the board's `attention:` lines and act on each, then launch what is ready, then schedule the next wake (`ScheduleWakeup`, 1200–1800 s, as a fallback; worker reports wake you sooner through session-coord). Do not read workers' transcripts or logs. If one item needs more, `foreman show <id>` gives its record and last events in a few lines.

**Before the first wake of a session**, attach so worker reports can reach you:

```
foreman attach --target "<this session's name>"
```

## Launching

```
foreman launch --ready          # prints exactly what would run; executes nothing
foreman launch --ready --go     # creates worktrees, opens tmux windows, starts claude
```

**The first launches of any plan are dry runs, shown to the user.** Real launches start only when the user has said the dry run looks right. After that, launching ready items is your job and needs no further permission unless the addendum says otherwise.

**A worker someone already started by hand** is adopted rather than relaunched, so foreman watches it like its own: find its session id with `session-coord board --repo <repo> --json`, then `foreman adopt <id> --session <sid> --pane <%N> --worktree <where it works>`. Nothing is launched.

The cap on live workers comes from config; `foreman` refuses past it and ready items queue. Do not try to get round it by launching by hand.

## Acting on attention

- **Worker gone.** Its session ended before the item finished. If it was mid-work, `foreman launch --relaunch <id> --go` resumes the same conversation in a new window. If the item needs rethinking, `foreman mark <id> reset --note "<why>"` returns it to ready.
- **No report for N hours.** Message the worker through session-coord and ask for a one-line status. Do not open its pane and read the scrollback.
- **Waiting on the user.** A worker paused at a gate inside its item. These lines are the board's account of what the user owes; gather them into your one line to the user rather than re-asking.
- **Stuck.** Read the one line. If you or a peer session can answer it, answer it; if it is the user's to decide, file one ask that stands on its own.
- **Claims done, oracle disagrees.** Usually the PR is open and not merged. Leave it. If the oracle is wrong, fix the oracle in the plan file rather than marking done by hand.
- **Late, or overdue.** The board gives the chain that cannot finish in time. Tell the user once, with the chain and the number of days, and what you would cut.
- **Merges queue in a repo.** Two items will land in one repo. Tell those workers to serialize with `session-coord lock merge`. foreman has no lock of its own, on purpose.
- **Decision not escalated.** The ask could not be filed; the line says why. Fix that, or file it yourself.

## Decisions

`foreman pulse` files one ask per ready decision item, with a stable tag, and never files the same one twice. When the user answers, record it and close the item:

```
foreman mark trust-posture done --note "pinned roots only, for the first release"
```

Then dissolve the ask in session-coord. Never close a decision on your own judgement, and never treat silence as an answer.

## Editing the plan

The plan is `plan.toml`, written by hand; `foreman check` validates it, and every verb refuses a plan with a cycle, a dangling dependency or a malformed oracle. You may edit it — add an item the work turned up, fix an oracle, add a dependency you discovered — and say in one line what you changed. Do not delete items that have state; mark them abandoned, so their dependents show as stranded rather than silently ready.

## If you are a worker

You were started with a prompt naming your plan and item. Read the briefs it names before anything else. Report with `foreman report --plan <plan> <item> <state> "<one line>"`:

- `running` at each real milestone, not every step;
- `review` with the PR URL once it is open;
- `done` when you believe the work is finished — foreman checks the exit condition itself, so a `done` before the merge leaves the item in review, which is correct;
- `waiting` when you pause for the user — a decision node to approve, a question you filed — naming what they must decide and any ask ids;
- `stuck` with what you need, when you cannot go on for any other reason.

One line each. The PM reads them every wake, and a paragraph costs it context every time.
