"""Auth setup tests — focus on env scrubbing + dual auth paths."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from audit import auth as auth_mod
from audit.auth import AuthError, configure_auth


def test_missing_everything_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Point auth at a guaranteed-absent credentials file
    monkeypatch.setattr(auth_mod, "CREDENTIALS_PATH", tmp_path / "no_creds.json")
    empty_env = tmp_path / ".env"
    empty_env.write_text("")
    with pytest.raises(AuthError, match="No subscription auth"):
        configure_auth(env_file=empty_env)


def test_oauth_token_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-deleted")
    monkeypatch.setattr(auth_mod, "CREDENTIALS_PATH", tmp_path / "no_creds.json")
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not installed in this environment")
    empty_env = tmp_path / ".env"
    empty_env.write_text("")
    status = configure_auth(env_file=empty_env)
    assert status.auth_mode == "oauth_token"
    assert status.api_key_scrubbed is True
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_keychain_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    monkeypatch.setattr(auth_mod, "CREDENTIALS_PATH", creds)
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not installed in this environment")
    empty_env = tmp_path / ".env"
    empty_env.write_text("")
    status = configure_auth(env_file=empty_env)
    assert status.auth_mode == "keychain_login"
    assert status.credentials_file == creds


def test_missing_claude_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-test-token")
    monkeypatch.setenv("PATH", "/nonexistent")
    empty_env = tmp_path / ".env"
    empty_env.write_text("")
    with pytest.raises(AuthError, match="claude.*CLI"):
        configure_auth(env_file=empty_env)
