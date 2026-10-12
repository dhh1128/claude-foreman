"""Stand-ins for the world: temp git repos with a bare origin, and fake `session-coord`, `tmux`
and `gh` executables on PATH that log every call.

The fakes are executables, not monkeypatches, so the acceptance tests drive the real `foreman`
command in a fresh process each time -- which is also how they show that nothing survives in
memory between calls.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin" / "foreman"

SESSION_COORD = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_DIR"]
a = sys.argv[1:]
with open(os.path.join(d, "session-coord.calls"), "a") as f:
    f.write(json.dumps(a) + "\n")
if a[:1] == ["board"]:
    p = os.path.join(d, "board.json")
    print(open(p).read() if os.path.exists(p) else "[]")
elif a[:2] == ["ask", "add"]:
    tag = a[a.index("--tag") + 1]
    p = os.path.join(d, "asks.txt")
    have = open(p).read().split() if os.path.exists(p) else []
    if tag in have:
        print(f"session-coord: {tag} is already in the queue; tags must be unique to be citable.", file=sys.stderr)
        sys.exit(2)
    open(p, "a").write(tag + "\n")
    print(f"filed {tag}")
elif a[:1] == ["msg"]:
    open(os.path.join(d, "msgs.txt"), "a").write(json.dumps(a) + "\n")
'''

TMUX = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_DIR"]
p = os.path.join(d, "tmux.calls")
n = sum(1 for _ in open(p)) if os.path.exists(p) else 0
with open(p, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
print(f"%{100 + n}")
'''

GH = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_DIR"]
num, repo = sys.argv[3], sys.argv[5]
merged = os.path.join(d, "merged.txt")
have = open(merged).read().split() if os.path.exists(merged) else []
print("MERGED" if f"{repo}#{num}" in have else "OPEN")
'''


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def git(*args: str, cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env=env).stdout


def make_repo(root: Path, name: str) -> Path:
    """A clone whose origin is a bare repo with one commit on main, origin/HEAD set."""
    bare = root / "origins" / f"{name}.git"
    git("init", "-q", "--bare", "-b", "main", str(bare))
    seed = root / "seed" / name
    git("init", "-q", "-b", "main", str(seed))
    (seed / "README").write_text(name)
    git("add", "README", cwd=seed)
    git("commit", "-q", "-m", "seed", cwd=seed)
    git("push", "-q", str(bare), "main", cwd=seed)
    clone = root / "repos" / name
    git("clone", "-q", str(bare), str(clone))
    return clone


class World:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.fake = tmp / "fake"
        self.fake.mkdir()
        bindir = tmp / "fakebin"
        bindir.mkdir()
        _exe(bindir / "session-coord", SESSION_COORD)
        _exe(bindir / "tmux", TMUX)
        _exe(bindir / "gh", GH)
        self.env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                    "FAKE_DIR": str(self.fake)}
        self.signals = tmp / "signals"
        self.signals.mkdir()

    def foreman(self, *args: str, env: dict | None = None, check: bool = True):
        r = subprocess.run([sys.executable, str(BIN), *args], capture_output=True, text=True,
                           env={**self.env, **(env or {})}, cwd=self.tmp)
        if check and r.returncode != 0:
            raise AssertionError(f"foreman {' '.join(args)} exited {r.returncode}:\n"
                                 f"{r.stdout}\n{r.stderr}")
        return r

    def calls(self, name: str) -> list[list[str]]:
        p = self.fake / f"{name}.calls"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def asks(self) -> list[str]:
        p = self.fake / "asks.txt"
        return p.read_text().split() if p.exists() else []

    def set_board(self, sessions: list[str]) -> None:
        (self.fake / "board.json").write_text(json.dumps([{"session_id": s} for s in sessions]))

    def signal(self, name: str) -> None:
        (self.signals / name).touch()
