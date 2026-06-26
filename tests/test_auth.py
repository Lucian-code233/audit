"""Auth preflight tests for the cursor `agent` CLI backend.

Modes: api_key (CURSOR_API_KEY / CURSOR_AUTH_TOKEN in env) and cli_login
(a stored `agent login` session detected via `agent status`).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from audit import auth as auth_mod
from audit.auth import AuthError, configure_auth


def _empty_env(tmp_path: Path) -> Path:
    p = tmp_path / ".env"
    p.write_text("")
    return p


def _clear_all_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def _fake_agent_on_path(monkeypatch: pytest.MonkeyPatch, path: str = "/usr/bin/agent") -> None:
    """Pretend the `agent` binary is on PATH without requiring it installed."""
    monkeypatch.setattr(auth_mod.shutil, "which",
                        lambda name: path if name == "agent" else shutil.which(name))


def _stub_agent_status(monkeypatch: pytest.MonkeyPatch, *, logged_in: bool) -> None:
    """Stub `agent status` (and `agent --version`) subprocess calls."""
    def fake_run(cmd, *a, **k):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        r = R()
        if "status" in cmd:
            r.stdout = "Logged in as test@example.com" if logged_in else "Not logged in"
            r.returncode = 0 if logged_in else 1
        elif "--version" in cmd:
            r.stdout = "cursor-agent 1.2.3"
        return r
    monkeypatch.setattr(auth_mod.subprocess, "run", fake_run)


# ---------- absence ----------


def test_missing_agent_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_all_auth_env(monkeypatch)
    monkeypatch.setattr(auth_mod.shutil, "which", lambda name: None)
    with pytest.raises(AuthError, match="agent.*CLI"):
        configure_auth(env_file=_empty_env(tmp_path))


def test_no_key_and_not_logged_in_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_all_auth_env(monkeypatch)
    _fake_agent_on_path(monkeypatch)
    _stub_agent_status(monkeypatch, logged_in=False)
    with pytest.raises(AuthError, match="No cursor auth available"):
        configure_auth(env_file=_empty_env(tmp_path))


# ---------- api_key mode ----------


def test_api_key_mode_cursor_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_all_auth_env(monkeypatch)
    _fake_agent_on_path(monkeypatch)
    _stub_agent_status(monkeypatch, logged_in=False)  # key should win regardless
    monkeypatch.setenv("CURSOR_API_KEY", "cur-sk-fake")
    status = configure_auth(env_file=_empty_env(tmp_path))
    assert status.auth_mode == "api_key"
    assert status.api_key_source == "CURSOR_API_KEY"
    assert status.agent_cli_version == "cursor-agent 1.2.3"


def test_api_key_mode_cursor_auth_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_all_auth_env(monkeypatch)
    _fake_agent_on_path(monkeypatch)
    _stub_agent_status(monkeypatch, logged_in=False)
    monkeypatch.setenv("CURSOR_AUTH_TOKEN", "cur-tok-fake")
    status = configure_auth(env_file=_empty_env(tmp_path))
    assert status.auth_mode == "api_key"
    assert status.api_key_source == "CURSOR_AUTH_TOKEN"


def test_api_key_outranks_cli_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An env key is detected before falling back to `agent status`."""
    _clear_all_auth_env(monkeypatch)
    _fake_agent_on_path(monkeypatch)
    _stub_agent_status(monkeypatch, logged_in=True)
    monkeypatch.setenv("CURSOR_API_KEY", "cur-sk-fake")
    status = configure_auth(env_file=_empty_env(tmp_path))
    assert status.auth_mode == "api_key"


# ---------- cli_login mode ----------


def test_cli_login_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_all_auth_env(monkeypatch)
    _fake_agent_on_path(monkeypatch)
    _stub_agent_status(monkeypatch, logged_in=True)
    status = configure_auth(env_file=_empty_env(tmp_path))
    assert status.auth_mode == "cli_login"
    assert status.api_key_source is None
