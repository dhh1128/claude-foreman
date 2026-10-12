"""What has happened: `state.json` and `events.jsonl`, beside the plan file.

The PM is a long-running Claude session, and it will be compacted, crash, or be killed by a
reboot. So nothing the PM knows may live only in its context. Every fact a later wake needs is
written here before the command that learned it returns, and a fresh PM pointed at the same
plan reads exactly what the old one would have.

Three rules, all inherited from session-coord, where each was learned by losing data:

- A missing file is empty. Anything else that cannot be read raises. A tolerant reader that
  returns {} for a file it could not parse hands the next write a licence to save {} over it.
- Every read-modify-write holds the plan's lock for the whole transaction, not just the write.
  An atomic rename prevents a torn read; it does not make the transaction atomic.
- Writes go to a temporary file and are renamed into place, so a process killed mid-write
  leaves the previous state intact rather than half a file.

The event log is append-only and is the audit trail; `state.json` is the summary every wake
reads. If the two ever disagree, the log is the record of what was reported and when.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

STATE = "state.json"
EVENTS = "events.jsonl"
LOCK = ".lock"
VERSION = 1


class StoreError(RuntimeError):
    """State on disk exists but cannot be trusted. Never treated as empty."""


def empty() -> dict[str, Any]:
    return {"version": VERSION, "items": {}, "pm": {}}


def read(plan_dir: Path) -> dict[str, Any]:
    p = plan_dir / STATE
    try:
        text = p.read_text()
    except FileNotFoundError:
        return empty()
    except OSError as e:
        raise StoreError(f"cannot read {p}: {e}") from None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise StoreError(f"{p} is not valid JSON ({e}). It was not overwritten; repair it or "
                         "move it aside, and the events log beside it holds the history.") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), dict):
        raise StoreError(f"{p} does not look like zamu state; it was not overwritten.")
    doc.setdefault("pm", {})
    return doc


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write(plan_dir: Path, doc: dict[str, Any]) -> None:
    _write_atomic(plan_dir / STATE, json.dumps(doc, indent=2, sort_keys=True) + "\n")


@contextlib.contextmanager
def locked(plan_dir: Path, wait: float = 30.0) -> Iterator[None]:
    """Hold the plan's lock. Raises TimeoutError after `wait` seconds rather than hanging."""
    plan_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(plan_dir / LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"another zamu process holds {plan_dir / LOCK}; "
                                       "retry in a moment.") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def transaction(plan_dir: Path, wait: float = 30.0) -> Iterator[dict[str, Any]]:
    """Read state under the lock, let the caller change it, write it back if it changed."""
    with locked(plan_dir, wait):
        doc = read(plan_dir)
        before = json.dumps(doc, sort_keys=True)
        yield doc
        if json.dumps(doc, sort_keys=True) != before:
            write(plan_dir, doc)


def append_event(plan_dir: Path, event: dict[str, Any]) -> None:
    """Append one line. Callers already hold the lock, so lines never interleave."""
    with open(plan_dir / EVENTS, "a") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")


def events(plan_dir: Path, item: str | None = None) -> list[dict[str, Any]]:
    """The log, tolerating only a torn final line -- the one damage an append can leave."""
    p = plan_dir / EVENTS
    try:
        lines = p.read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for n, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            if n == len(lines) - 1:
                break
            raise StoreError(f"{p}:{n + 1} is not valid JSON.") from None
        if item is None or ev.get("item") == item:
            out.append(ev)
    return out
