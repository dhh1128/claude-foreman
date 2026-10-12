"""Exit oracles: machine-checkable done-conditions, checked by zamu rather than believed.

A worker saying "done" is a claim. An oracle is the check. The PM never marks an item done on
a worker's word, because the cost of a wrong "done" is a dependent launched onto a base that
does not have the change -- and nobody notices until that dependent's work is half built.

Spec forms, one string each; an item may list several and all must hold:

    pr:owner/repo#123            the pull request is merged (via `gh`)
    pr:https://github.com/o/r/pull/123
    cmd:<shell command>          exits 0, run in the item's repo (or the plan's directory)
    file:<path>                  exists
    manual                       never true by itself; `zamu mark <id> done` closes it

Every check answers True, False or None, and None means *could not look* -- gh not installed,
a timeout, a network error. That is never treated as False, and never as True. The rule is
session-coord's: an unavailable observation is not an observation of absence. A PM that read a
gh outage as "not merged" would only delay; one that read it as merged would launch dependents.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

#: (argv, cwd, timeout) -> (returncode, stdout). Raises OSError or TimeoutExpired on failure.
Runner = Callable[[Sequence[str], str, float], "tuple[int, str]"]

CMD_TIMEOUT = 60.0
PR_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)/?$")
PR_SHORT = re.compile(r"^([^/\s#]+/[^/\s#]+)#(\d+)$")


class OracleError(ValueError):
    pass


@dataclass(frozen=True)
class Verdict:
    ok: bool | None
    detail: str


def default_runner(argv: Sequence[str], cwd: str, timeout: float) -> tuple[int, str]:
    r = subprocess.run(list(argv), cwd=cwd or None, capture_output=True, text=True,
                       timeout=timeout)
    return r.returncode, r.stdout


def parse_spec(spec: str) -> tuple[str, str]:
    s = spec.strip()
    if s == "manual":
        return "manual", ""
    kind, sep, arg = s.partition(":")
    if not sep or kind not in ("pr", "cmd", "file") or not arg.strip():
        raise OracleError(f"oracle {spec!r} is not one of pr:owner/repo#N, cmd:<shell>, "
                          "file:<path>, manual.")
    arg = arg.strip()
    if kind == "pr" and not (PR_URL.match(arg) or PR_SHORT.match(arg)):
        raise OracleError(f"oracle {spec!r}: write the PR as owner/repo#N or its URL.")
    return kind, arg


def check_one(spec: str, cwd: str, run: Runner = default_runner) -> Verdict:
    kind, arg = parse_spec(spec)
    if kind == "manual":
        return Verdict(False, "manual: closed only by `zamu mark <id> done`")
    if kind == "file":
        p = Path(os.path.expanduser(arg))
        try:
            return Verdict(p.exists(), f"{p} {'exists' if p.exists() else 'is absent'}")
        except OSError as e:
            return Verdict(None, f"could not stat {p}: {e}")
    if kind == "cmd":
        try:
            rc, _ = run(["sh", "-c", arg], cwd, CMD_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            return Verdict(None, f"could not run `{arg}`: {e.__class__.__name__}")
        return Verdict(rc == 0, f"`{arg}` exited {rc}")
    m = PR_URL.match(arg) or PR_SHORT.match(arg)
    repo, num = m.group(1), m.group(2)
    try:
        rc, out = run(["gh", "pr", "view", num, "-R", repo, "--json", "state", "-q", ".state"],
                      cwd, CMD_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return Verdict(None, f"could not ask gh about {repo}#{num}: {e.__class__.__name__}")
    if rc != 0:
        return Verdict(None, f"gh could not read {repo}#{num} (exit {rc})")
    state = out.strip().upper()
    return Verdict(state == "MERGED", f"{repo}#{num} is {state.lower() or 'unknown'}")


def check(specs: Sequence[str], cwd: str, run: Runner = default_runner) -> Verdict:
    """All must hold. Any False is False; otherwise any None is None."""
    if not specs:
        return Verdict(False, "no oracle")
    verdicts = [check_one(s, cwd, run) for s in specs]
    detail = "; ".join(v.detail for v in verdicts)
    if any(v.ok is False for v in verdicts):
        return Verdict(False, detail)
    if any(v.ok is None for v in verdicts):
        return Verdict(None, detail)
    return Verdict(True, detail)


def is_manual(specs: Sequence[str]) -> bool:
    return any(s.strip() == "manual" for s in specs)
