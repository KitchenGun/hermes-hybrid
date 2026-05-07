"""Tests for hermes-setup CLI (Phase 19, 2026-05-07).

Locks down:
  * HERMES_NO_AUTO_TIMER → silent skip
  * /.dockerenv presence → skip
  * auto_timer_enabled=False → skip
  * --dry-run prints plan without registering
  * platform dispatch picks the right handler module
  * non-interactive without ack → skip
  * Windows handler issues schtasks /Create per task
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.cli import setup as cli_setup
from src.cli.timer_handlers import windows as win_handler


def _fake_settings(*, enabled: bool = True, ack: bool = False) -> SimpleNamespace:
    return SimpleNamespace(auto_timer_enabled=enabled, auto_timer_ack=ack)


# ---- env / container guards ------------------------------------------


def test_main_skips_when_HERMES_NO_AUTO_TIMER_set(capsys, monkeypatch):
    monkeypatch.setenv("HERMES_NO_AUTO_TIMER", "1")
    with patch.object(cli_setup, "_import_handler") as h:
        rc = cli_setup.main([])
    assert rc == 0
    h.assert_not_called()
    out = capsys.readouterr().out
    assert "skipping" in out


def test_main_skips_in_docker_container(capsys, monkeypatch):
    monkeypatch.delenv("HERMES_NO_AUTO_TIMER", raising=False)
    with patch.object(cli_setup, "_is_container", return_value=True), \
         patch.object(cli_setup, "_import_handler") as h:
        rc = cli_setup.main([])
    assert rc == 0
    h.assert_not_called()
    assert "container" in capsys.readouterr().out


def test_main_skips_when_auto_timer_disabled(capsys, monkeypatch):
    monkeypatch.delenv("HERMES_NO_AUTO_TIMER", raising=False)
    with patch.object(cli_setup, "_is_container", return_value=False), \
         patch.object(cli_setup, "get_settings", return_value=_fake_settings(enabled=False)), \
         patch.object(cli_setup, "_import_handler") as h:
        rc = cli_setup.main([])
    assert rc == 0
    h.assert_not_called()
    assert "auto_timer_enabled=false" in capsys.readouterr().out


# ---- platform dispatch ----------------------------------------------


def test_platform_module_name_dispatches():
    with patch.object(sys, "platform", "win32"):
        assert cli_setup._platform_module_name() == "windows"
    with patch.object(sys, "platform", "darwin"):
        assert cli_setup._platform_module_name() == "darwin"
    with patch.object(sys, "platform", "linux"):
        assert cli_setup._platform_module_name() == "linux"


def test_platform_module_name_unsupported_raises():
    with patch.object(sys, "platform", "freebsd"):
        with pytest.raises(RuntimeError):
            cli_setup._platform_module_name()


# ---- dry-run ---------------------------------------------------------


def test_dry_run_prints_plan_without_registering(capsys, monkeypatch):
    monkeypatch.delenv("HERMES_NO_AUTO_TIMER", raising=False)

    fake_plan = [["echo", "hello"]]

    class _FakeHandler:
        @staticmethod
        def plan(repo):
            return fake_plan

        @staticmethod
        def register(repo, *, ack):
            raise AssertionError("dry-run must not call register()")

    with patch.object(cli_setup, "_is_container", return_value=False), \
         patch.object(cli_setup, "get_settings", return_value=_fake_settings()), \
         patch.object(cli_setup, "_import_handler", return_value=_FakeHandler):
        rc = cli_setup.main(["--dry-run"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "echo hello" in out


# ---- non-interactive without ack ------------------------------------


def test_non_interactive_without_ack_skips_register(capsys, monkeypatch):
    """ack=False + --non-interactive → skip register() but exit 0."""
    monkeypatch.delenv("HERMES_NO_AUTO_TIMER", raising=False)

    register_called: list[bool] = []

    class _FakeHandler:
        @staticmethod
        def plan(repo):
            return [["echo", "task1"]]

        @staticmethod
        def register(repo, *, ack):
            register_called.append(ack)
            return ["task1"]

    with patch.object(cli_setup, "_is_container", return_value=False), \
         patch.object(cli_setup, "get_settings", return_value=_fake_settings(ack=False)), \
         patch.object(cli_setup, "_import_handler", return_value=_FakeHandler):
        rc = cli_setup.main(["--non-interactive"])

    assert rc == 0
    assert register_called == []                 # skipped


def test_ack_true_skips_prompt_and_calls_register(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_NO_AUTO_TIMER", raising=False)

    captured: list[bool] = []

    class _FakeHandler:
        @staticmethod
        def plan(repo):
            return [["echo", "x"]]

        @staticmethod
        def register(repo, *, ack):
            captured.append(ack)
            return ["x"]

    with patch.object(cli_setup, "_is_container", return_value=False), \
         patch.object(cli_setup, "get_settings", return_value=_fake_settings(ack=True)), \
         patch.object(cli_setup, "_repo_root", return_value=tmp_path), \
         patch.object(cli_setup, "_import_handler", return_value=_FakeHandler):
        rc = cli_setup.main([])

    assert rc == 0
    assert captured == [True]


# ---- Windows handler ------------------------------------------------


def test_windows_plan_emits_three_schtasks_commands(tmp_path):
    plans = win_handler.plan(tmp_path)
    assert len(plans) == 3
    names = []
    for cmd in plans:
        assert cmd[0] == "schtasks"
        assert "/Create" in cmd
        assert "WEEKLY" in cmd
        assert "SUN" in cmd
        names.append(cmd[cmd.index("/TN") + 1])
    assert names == ["HermesReflection", "HermesCurator", "HermesPromoter"]


def test_windows_register_calls_subprocess_per_task(tmp_path, monkeypatch):
    """register should subprocess.run once per task and collect successes."""
    import subprocess

    captured: list[list[str]] = []

    class _OK:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd, **kw):
        captured.append(list(cmd))
        return _OK()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    registered = win_handler.register(tmp_path, ack=True)
    assert len(registered) == 3
    assert len(captured) == 3
    # All three task names land in the registered list
    assert {"HermesReflection", "HermesCurator", "HermesPromoter"} == set(registered)


def test_windows_register_records_failures_but_continues(tmp_path, monkeypatch):
    import subprocess

    class _FailFirst:
        def __init__(self):
            self.calls = 0

        def __call__(self, cmd, **kw):
            self.calls += 1
            class _Result:
                pass
            r = _Result()
            r.stdout = ""
            if self.calls == 1:
                r.returncode = 1
                r.stderr = "permission denied"
            else:
                r.returncode = 0
                r.stderr = ""
            return r

    monkeypatch.setattr(subprocess, "run", _FailFirst())
    registered = win_handler.register(tmp_path, ack=True)
    # First failed, the other two succeed.
    assert len(registered) == 2


def test_register_without_ack_is_noop(tmp_path):
    assert win_handler.register(tmp_path, ack=False) == []
