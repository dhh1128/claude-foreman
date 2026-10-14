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
- Claude runs as a child of a shell in the tmux pane so session-coord can see it. The shell
  remains when Claude exits, while the session disappears from session-coord's board.
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
    token: str = ""          # carried in the wrapper's argv; proves the worker by its pane
    tmux_session: str = ""   # for PM launches, recorded again once the window is open
    pm_target: str = ""      # PM display name, also used as the session-coord target
    pm_guard: tuple[str, str, str] = ("", "", "")  # target, status, since before a PM launch
    engine: str = "claude"
    parked: bool = False


def worker_prompt(plan: Plan, it: Item, worktree: str, resume: bool, changed: str = "") -> str:
    briefs = " then ".join(p for p in (plan.common_brief, it.brief) if p) or "(no brief given)"
    oracle = " AND ".join(it.oracle) if it.oracle else "none"
    rep = f"zamu report --plan {shlex.quote(plan.name)} {it.id}"
    if resume:
        head = (f"You were relaunched as the worker for zamu plan '{plan.name}', item "
                f"'{it.id}'. Your previous session ended; check the worktree and branch state "
                f"before continuing.")
        if changed:
            head += f" Since you were parked, {changed}."
    else:
        head = f"You are the worker session for zamu plan '{plan.name}', item '{it.id}': {it.title}."
    return (
        f"{head} Read {briefs} before doing anything else. You are in the worktree {worktree} "
        f"on branch {it.topic}. Report to the project manager with `{rep} running \"<one "
        f"line>\"` at each milestone, `{rep} review \"<PR url>\"` once your PR is open, `{rep} "
        f"done \"<one line>\"` when you believe the work is finished, `{rep} waiting \"<what the "
        f"user must decide, with any ask ids>\"` when you pause for the user's approval, and `{rep} stuck \"<what "
        f"you need>\"` if you cannot proceed. If the command fails, append a line to "
        f"{worktree}/.zamu/reports instead: `<ISO time> <state> <one line>`, using the same "
        f"states. End every report line with a newline. zamu checks the exit condition itself and "
        f"does not take your word for it: {oracle}. Merging, review, and anything needing the "
        f"user follow your normal rules; zamu will not do them for you. You were started "
        f"in a background window that nobody is watching, so do not stop to ask the user "
        f"anything before you begin, even if a brief says to; if you genuinely cannot proceed "
        f"without them, report stuck and say what you need. If you hand work to Codex or another "
        f"tool, make sure the processes it started (test runners especially) are gone before you "
        f"report review or done; an exited tool can leave them running orphaned in your worktree."
    )


def plan_pm_launch(plan: Plan, repo: str, tmux_session: str, claude: Sequence[str]) -> Launch:
    """Build a fresh PM launch from the brief stored beside the plan."""
    sid = str(uuid.uuid4())
    name = f"{plan.name}-prj"
    prompt = (f"You are the project manager for zamu plan '{plan.name}' in a background "
              f"window. Read {plan.path.parent / 'pm.md'} before doing anything else. "
              f"Load the zamu skill, run `zamu attach --plan {shlex.quote(plan.name)} "
              f"--repo {shlex.quote(repo)} --target {shlex.quote(name)}`, then run "
              f"`zamu pulse --plan {shlex.quote(plan.name)}`. Continue from the plan's "
              "on-disk state and brief; this is a fresh session.")
    cmd = [*claude, "-n", name, "--session-id", sid, prompt]
    shell = ["sh", "-c", '"$@"; exec "${SHELL:-/bin/sh}" -l', f"zamu-pm:{sid}", *cmd]
    tmux = ("tmux", "new-window", "-d", "-P", "-F", "#{pane_id}", "-t",
            f"{tmux_session}:", "-n", name, "-c", repo, *shell)
    return Launch("pm", sid, repo, "", False,
                  (Step(tmux, "open a window running the PM", capture="pane"),),
                  token=sid, tmux_session=tmux_session, pm_target=name)


def plan_launch(plan: Plan, it: Item, *, tmux_session: str, claude: Sequence[str],
                default_branch: str, worktree_exists: bool, branch_exists: bool,
                session: str | None = None, worktree: str = "", engine: str = "claude",
                changed: str = "", parked: bool = False) -> Launch:
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
    steps.append(Step(("mkdir", "-p", f"{wt}/.zamu"), "prepare private worker reports"))
    steps.append(Step(("sh", "-c", 'test -e "$1" || printf "*\\n" > "$1"',
                       "zamu-ignore", f"{wt}/.zamu/.gitignore"),
                      "keep worker reports out of git"))
    target = f"{tmux_session}:" if tmux_session else ""
    if engine == "claude":
        cmd = list(claude) + (["--resume", sid] if resume else ["--session-id", sid])
    else:
        cmd = list(claude)  # other engines take the prompt as their first argument
        if engine == "codex":
            if resume:
                cmd += ["resume", sid]
            # Codex otherwise stops at an unattended "Trust this folder?" prompt. The
            # command-line config applies to this launch without editing the user's config.
            import json
            for root in (repo, wt):
                cmd += ["-c", f'projects.{json.dumps(root)}.trust_level="trusted"']
    cmd.append(worker_prompt(plan, it, wt, resume, changed))
    tmux = ["tmux", "new-window", "-d", "-P", "-F", "#{pane_id}"]
    if target:
        tmux += ["-t", target]
    # claude runs as a CHILD of a shell, not as the pane's own process. session-coord finds a
    # pane's claude among the pane process's descendants, so a pane whose process IS claude
    # never showed on its board, and the record was swept as stale (first real plan,
    # 2026-10-12). The trailing exec leaves a shell in the worktree when claude exits, which is
    # also what a hand-started window looks like.
    # $0 carries a token that lives exactly as long as the engine does: when the engine exits,
    # the exec replaces this argv. That proves liveness by pane for engines session-coord
    # cannot see.
    shell = ["sh", "-c", '"$@"; exec "${SHELL:-/bin/sh}" -l', f"zamu-worker:{sid}", *cmd]
    tmux += ["-n", it.topic, "-c", wt, *shell]
    steps.append(Step(tuple(tmux), "open a window running the worker", capture="pane"))
    return Launch(it.id, sid, wt, it.topic, resume, tuple(steps), token=sid, engine=engine,
                  parked=parked)


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
