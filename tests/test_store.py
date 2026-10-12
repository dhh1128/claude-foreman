from __future__ import annotations

import os

import pytest

from zamu import store


def test_missing_state_is_empty(tmp_path):
    assert store.read(tmp_path) == store.empty()


def test_unparseable_state_raises_and_is_not_overwritten(tmp_path):
    (tmp_path / store.STATE).write_text("{ torn")
    with pytest.raises(store.StoreError, match="not overwritten"):
        with store.transaction(tmp_path) as doc:
            doc["items"]["a"] = {"state": "done"}
    assert (tmp_path / store.STATE).read_text() == "{ torn"


def test_a_write_that_dies_before_the_rename_leaves_the_old_state(tmp_path, monkeypatch):
    with store.transaction(tmp_path) as doc:
        doc["items"]["a"] = {"state": "running"}
    before = (tmp_path / store.STATE).read_text()

    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        with store.transaction(tmp_path) as doc:
            doc["items"]["a"] = {"state": "done"}
    assert (tmp_path / store.STATE).read_text() == before
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("state.json.")] == []


def test_an_unchanged_transaction_does_not_write(tmp_path):
    with store.transaction(tmp_path):
        pass
    assert not (tmp_path / store.STATE).exists()


def test_events_tolerate_only_a_torn_last_line(tmp_path):
    store.append_event(tmp_path, {"item": "a", "what": "x"})
    with open(tmp_path / store.EVENTS, "a") as f:
        f.write('{"item": "b", "wh')
    assert [e["item"] for e in store.events(tmp_path)] == ["a"]
    (tmp_path / store.EVENTS).write_text('garbage\n{"item": "a"}\n')
    with pytest.raises(store.StoreError):
        store.events(tmp_path)


def test_the_lock_times_out_rather_than_hanging(tmp_path):
    with store.locked(tmp_path):
        with pytest.raises(TimeoutError):
            with store.locked(tmp_path, wait=0.1):
                pass
