from __future__ import annotations

import subprocess

import pytest

from zamu.oracles import OracleError, check, check_one, parse_spec


def runner(table):
    calls = []

    def run(argv, cwd, timeout):
        calls.append(list(argv))
        r = table(list(argv))
        if isinstance(r, BaseException):
            raise r
        return r
    run.calls = calls
    return run


def test_file_oracle(tmp_path):
    f = tmp_path / "flag"
    assert check_one(f"file:{f}", "").ok is False
    f.touch()
    assert check_one(f"file:{f}", "").ok is True


def test_cmd_oracle_uses_exit_status_and_cwd(tmp_path):
    assert check_one("cmd:test -d .", str(tmp_path)).ok is True
    assert check_one("cmd:false", str(tmp_path)).ok is False


def test_cmd_timeout_is_could_not_look_not_false():
    run = runner(lambda a: subprocess.TimeoutExpired(a, 1))
    assert check_one("cmd:sleep 100", "", run).ok is None


@pytest.mark.parametrize("spec", ["pr:acme/core#12", "pr:https://github.com/acme/core/pull/12"])
def test_pr_oracle_asks_gh_for_merged(spec):
    run = runner(lambda a: (0, "MERGED\n"))
    v = check_one(spec, "", run)
    assert v.ok is True
    assert run.calls == [["gh", "pr", "view", "12", "-R", "acme/core", "--json", "state",
                          "-q", ".state"]]
    assert check_one(spec, "", runner(lambda a: (0, "OPEN\n"))).ok is False


def test_gh_failure_is_could_not_look():
    assert check_one("pr:a/b#1", "", runner(lambda a: (1, ""))).ok is None
    assert check_one("pr:a/b#1", "", runner(lambda a: FileNotFoundError("gh"))).ok is None


def test_manual_is_never_true():
    assert check_one("manual", "").ok is False


@pytest.mark.parametrize("spec", ["merged", "pr:core", "cmd:", "url:x", "pr:a/b#x"])
def test_bad_specs(spec):
    with pytest.raises(OracleError):
        parse_spec(spec)


def test_all_must_hold_and_unknown_does_not_become_false(tmp_path):
    yes = f"file:{tmp_path}"
    unknown = "pr:a/b#1"
    run = runner(lambda a: (1, ""))
    assert check([yes, unknown], "", run).ok is None
    assert check([yes, "manual", unknown], "", run).ok is False
    assert check([yes], "", run).ok is True
