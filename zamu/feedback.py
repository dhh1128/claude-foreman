"""Append-only field observations shared by all plans."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

from . import config, store

FIELD_NOTES = Path("/home/daniel/code/me/claude-zamu/.ignored/field-notes.md")
NOTE = re.compile(r"^- (\d{4}-\d{2}-\d{2})\s+([^:]+):\s*(.*)$")


def path() -> Path:
    return config.home() / "feedback.jsonl"


def _read() -> list[dict]:
    p = path()
    try:
        lines = p.read_text().splitlines()
    except FileNotFoundError:
        return []
    except OSError as e:
        raise store.StoreError(f"cannot read {p}: {e}") from None
    rows = []
    for index, line in enumerate(lines):
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get("t"), str):
                raise ValueError("not a feedback record")
            rows.append(row)
        except (ValueError, TypeError):
            raise store.StoreError(f"{p}:{index + 1} is not valid feedback JSON") from None
    return rows


def _append(row: dict) -> None:
    with path().open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def _old_notes(source: Path) -> list[dict]:
    try:
        lines = source.read_text().splitlines()
    except FileNotFoundError:
        return []
    except OSError as e:
        raise store.StoreError(f"cannot read {source}: {e}") from None
    notes = []
    for line in lines:
        match = NOTE.match(line)
        if match:
            day, name, body = match.groups()
            notes.append({"t": day + "T00:00:00+00:00", "plan": "", "pane": "",
                          "name": name, "text": body})
        elif notes and line[:1].isspace() and line.strip():
            notes[-1]["text"] += " " + line.strip()
    for note in notes:
        note["source"] = "field-notes:" + hashlib.sha256(
            (note["t"] + "\0" + note["name"] + "\0" + note["text"]).encode()).hexdigest()
    return notes


def _migrate_locked(source: Path) -> int:
    existing = {row.get("source") for row in _read()}
    count = 0
    for row in _old_notes(source):
        if row["source"] not in existing:
            _append(row)
            existing.add(row["source"])
            count += 1
    return count


def migrate(source: Path | None = None) -> int:
    config.home().mkdir(parents=True, exist_ok=True)
    with store.locked(config.home()):
        return _migrate_locked(source or FIELD_NOTES)


def add(plan: str, message: str, now: dt.datetime, *, pane: str = "", name: str = "") -> dict:
    if not message.strip():
        raise ValueError("feedback text must not be empty")
    config.home().mkdir(parents=True, exist_ok=True)
    row = {"t": now.isoformat(), "plan": plan, "pane": pane, "name": name,
           "text": message.strip()}
    with store.locked(config.home()):
        _migrate_locked(FIELD_NOTES)
        _append(row)
    return row


def list_feedback(since: dt.datetime | None = None, *, limit: int | None = None) -> list[dict]:
    migrate()
    with store.locked(config.home()):
        rows = _read()
    if since:
        rows = [row for row in rows if dt.datetime.fromisoformat(row["t"]) >= since]
    ordered = sorted(enumerate(rows), key=lambda indexed: (indexed[1]["t"], indexed[0]),
                     reverse=True)
    return [row for _index, row in (ordered[:limit] if limit is not None else ordered)]
