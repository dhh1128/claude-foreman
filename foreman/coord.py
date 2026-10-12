"""The session-coord adapter: liveness, the ask queue, and waking the PM.

foreman reuses session-coord rather than reimplementing any of it. Liveness in particular took
session-coord eleven review rounds to get right -- pid recycling, tmux server restarts, panes
that hold a different claude than the one recorded -- and a second copy would drift from the
first the day either changed. So a worker is live iff session-coord's own board, which sweeps
stale records by that logic, still lists its session id.

Everything here goes through the `session-coord` command, never its Python, because the two
plugins are installed and upgraded separately. Each call can fail, and every failure is
reported as *could not look* (None), never as an empty answer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

#: argv -> (returncode, stdout, stderr). Raises OSError / TimeoutExpired when it cannot run.
Exec = Callable[[Sequence[str]], "tuple[int, str, str]"]

TIMEOUT = 30.0


def default_exec(argv: Sequence[str]) -> tuple[int, str, str]:
    r = subprocess.run(list(argv), capture_output=True, text=True, timeout=TIMEOUT)
    return r.returncode, r.stdout, r.stderr


@dataclass
class Coord:
    run: Exec = default_exec
    binary: str = "session-coord"

    def available(self) -> bool:
        return shutil.which(self.binary) is not None or self.run is not default_exec

    def _call(self, argv: Sequence[str]) -> tuple[int, str, str] | None:
        try:
            return self.run([self.binary, *argv])
        except (OSError, subprocess.TimeoutExpired):
            return None

    def live_sessions(self, repo: str) -> set[str] | None:
        """Session ids session-coord considers live in `repo`. None = could not look."""
        r = self._call(["board", "--repo", repo, "--json"])
        if r is None or r[0] != 0:
            return None
        try:
            rows = json.loads(r[1] or "[]")
        except json.JSONDecodeError:
            return None
        if not isinstance(rows, list):
            return None
        return {str(x.get("session_id", "")) for x in rows if isinstance(x, dict)}

    def ask_add(self, repo: str, tag: str, kind: str, text: str,
                depends_on: Sequence[str] = ()) -> tuple[bool, str]:
        """File one ask. (True, note) when it is in the queue, including already there."""
        argv = ["ask", "add", "--repo", repo, "--tag", tag, "--kind", kind, "--text", text]
        for d in depends_on:
            argv += ["--depends-on", d]
        r = self._call(argv)
        if r is None:
            return False, "session-coord could not be run"
        rc, out, err = r
        if rc == 0 and f"filed {tag}" in out:
            return True, f"filed {tag}"
        if "already in the queue" in err:
            return True, f"{tag} was already filed"
        # `ask add` outside a git repo prints "not a git repo" and exits 0 having filed
        # nothing. Matching on the confirmation rather than the exit status is what stops that
        # from being recorded as an escalation that happened.
        return False, (err or out).strip() or f"session-coord exited {rc}"

    def msg(self, repo: str, target: str, text: str) -> tuple[bool, str]:
        r = self._call(["msg", "--repo", repo, target, text])
        if r is None:
            return False, "session-coord could not be run"
        return r[0] == 0, (r[2] or r[1]).strip()
