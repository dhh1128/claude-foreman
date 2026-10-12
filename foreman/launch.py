"""Launching a worker: a worktree, a tmux window, and a claude started with a known session id.

The commands are built here as data and executed elsewhere, so a dry run prints exactly what a
real run would execute -- the same list, not a description of it.

Three choices worth knowing:

- The worktree is `<repo>/.worktrees/<topic>`, branched from origin's default branch. Never
  `/tmp`, which is reaped, and never the main checkout, which other sessions share.
- claude is started with `--session-id <uuid>` chosen here, so the PM knows the worker's
  session id before the worker has said anything, and liveness can be checked against
  session-coord's board by id. A relaunch of the same item uses `--resume <uuid>`, so the
  worker comes back with its own conversation rather than starting over.
- The window runs claude directly, not a shell, so when claude exits the window closes and
  session-coord sees the session gone. That is what makes a dead worker detectable at all.
"""

from __future__ import annotations

import shlex
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .plan import Item, Plan


@dataclass(frozen=True)
class Step:
    argv: tuple[str, ...]
    why: str
    capture: str = ""        # name under which this step's stdout is recorded

    def shell(self) -> str:
        return shlex.join(self.argv)


@dataclass(frozen=True)
class Launch:
    item: str
    session: str
    worktree: str
    branch: str
    resume: bool
    steps: tuple[Step, ...] = field(default=())


def worker_prompt(plan: Plan, it: Item, worktree: str, resume: bool) -> str:
    briefs = " then ".join(p for p in (plan.common_brief, it.brief) if p) or "(no brief given)"
    oracle = " AND ".join(it.oracle) if it.oracle else "none"
    rep = f"foreman report --plan {shlex.quote(plan.name)} {it.id}"
    head = (f"You were relaunched as the worker for foreman plan '{plan.name}', item "
            f"'{it.id}'. Your previous session ended; check the worktree and branch state "
            f"before continuing." if resume else
            f"You are the worker session for foreman plan '{plan.name}', item '{it.id}': "
            f"{it.title}.")
    return (
        f"{head} Read {briefs} before doing anything else. You are in the worktree {worktree} "
        f"on branch {it.topic}. Report to the project manager with `{rep} running \"<one "
        f"line>\"` at each milestone, `{rep} review \"<PR url>\"` once your PR is open, `{rep} "
        f"done \"<one line>\"` when you believe the work is finished, `{rep} waiting \"<what the "
        f"user must decide, with any ask ids>\"` when you pause for the user's approval, and `{rep} stuck \"<what "
        f"you need>\"` if you cannot proceed. foreman checks the exit condition itself and "
        f"does not take your word for it: {oracle}. Merging, review, and anything needing the "
        f"user follow your normal rules; foreman will not do them for you. You were started "
        f"in a background window that nobody is watching, so do not stop to ask the user "
        f"anything before you begin, even if a brief says to; if you genuinely cannot proceed "
        f"without them, report stuck and say what you need."
    )


def plan_launch(plan: Plan, it: Item, *, tmux_session: str, claude: Sequence[str],
                default_branch: str, worktree_exists: bool, branch_exists: bool,
                session: str | None = None, worktree: str = "") -> Launch:
    """The exact commands for one launch. `session` given means resume that conversation.

    `worktree` names a tree the worker already used -- an adopted worker may have run in the
    main checkout -- and is reused as it is, with no git steps.
    """
    repo = it.repo.rstrip("/")
    wt = worktree or f"{repo}/.worktrees/{it.topic}"
    if worktree:
        worktree_exists = True
    base = it.base or default_branch
    resume = session is not None
    sid = session or str(uuid.uuid4())
    steps: list[Step] = []
    if not worktree_exists:
        steps.append(Step(("git", "-C", repo, "fetch", "origin", base),
                          f"bring origin/{base} up to date"))
        if branch_exists:
            steps.append(Step(("git", "-C", repo, "worktree", "add", wt, it.topic),
                              f"check out the existing branch {it.topic}"))
        else:
            steps.append(Step(("git", "-C", repo, "worktree", "add", wt, "-b", it.topic,
                               f"origin/{base}"), f"new branch {it.topic} from origin/{base}"))
    target = f"{tmux_session}:" if tmux_session else ""
    cmd = list(claude) + (["--resume", sid] if resume else ["--session-id", sid])
    cmd.append(worker_prompt(plan, it, wt, resume))
    tmux = ["tmux", "new-window", "-d", "-P", "-F", "#{pane_id}"]
    if target:
        tmux += ["-t", target]
    # claude runs as a CHILD of a shell, not as the pane's own process. session-coord finds a
    # pane's claude among the pane process's descendants, so a pane whose process IS claude
    # never showed on its board, and the record was swept as stale (first real plan,
    # 2026-10-12). The trailing exec leaves a shell in the worktree when claude exits, which is
    # also what a hand-started window looks like.
    shell = ["sh", "-c", '"$@"; exec "${SHELL:-/bin/sh}" -l', "foreman-worker", *cmd]
    tmux += ["-n", it.topic, "-c", wt, *shell]
    steps.append(Step(tuple(tmux), "open a window running the worker", capture="pane"))
    return Launch(it.id, sid, wt, it.topic, resume, tuple(steps))


# --- facts about the repo, gathered before planning ---------------------------------------

Run = Callable[[Sequence[str]], "tuple[int, str, str]"]


def default_run(argv: Sequence[str]) -> tuple[int, str, str]:
    r = subprocess.run(list(argv), capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout, r.stderr


def default_branch(repo: str, run: Run = default_run) -> str:
    rc, out, _ = run(["git", "-C", repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if rc == 0 and out.strip().startswith("origin/"):
        return out.strip()[len("origin/"):]
    for b in ("main", "master"):
        rc, _, _ = run(["git", "-C", repo, "rev-parse", "--verify", "--quiet",
                        f"refs/remotes/origin/{b}"])
        if rc == 0:
            return b
    raise LaunchError(f"cannot tell origin's default branch in {repo}; set `base` on the item "
                      "or run `git -C <repo> remote set-head origin -a`.")


def branch_exists(repo: str, branch: str, run: Run = default_run) -> bool:
    rc, _, _ = run(["git", "-C", repo, "rev-parse", "--verify", "--quiet",
                    f"refs/heads/{branch}"])
    return rc == 0


class LaunchError(RuntimeError):
    pass


def execute(launch: Launch, run: Run = default_run) -> dict[str, str]:
    """Run the steps in order, stopping at the first failure. Returns captured outputs."""
    got: dict[str, str] = {}
    for s in launch.steps:
        try:
            rc, out, err = run(s.argv)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise LaunchError(f"{s.why}: could not run `{s.shell()}`: {e}") from None
        if rc != 0:
            raise LaunchError(f"{s.why}: `{s.shell()}` exited {rc}: {(err or out).strip()}")
        if s.capture:
            got[s.capture] = out.strip()
    return got


def worktree_exists(path: str) -> bool:
    return Path(path).is_dir()
