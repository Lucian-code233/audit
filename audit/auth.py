"""OAuth subscription auth setup.

Claude Code's authentication-precedence list (documented at
https://code.claude.com/docs/en/authentication#authentication-precedence)
ranks ANTHROPIC_API_KEY ABOVE the subscription OAuth token. If both are
set, API-key billing wins silently. This module scrubs the API-key
variables from the process environment and verifies that *some*
subscription auth is available — either an explicit
CLAUDE_CODE_OAUTH_TOKEN (preferred for CI / scripts) or a stored
keychain login from `claude login` (~/.claude/.credentials.json).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class AuthStatus:
    auth_mode: str            # "oauth_token" | "keychain_login" | "none"
    api_key_scrubbed: bool
    claude_cli_path: str | None
    claude_cli_version: str | None
    credentials_file: Path | None


class AuthError(RuntimeError):
    pass


CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


def configure_auth(env_file: Path | None = None) -> AuthStatus:
    """Load .env, scrub API-key env vars, verify subscription auth + claude CLI.

    Accepts either CLAUDE_CODE_OAUTH_TOKEN or an existing `claude login`
    keychain entry. Raises AuthError on neither.
    """
    if env_file is not None and env_file.exists():
        load_dotenv(env_file)
    else:
        load_dotenv()

    api_key_was_set = "ANTHROPIC_API_KEY" in os.environ
    if api_key_was_set:
        del os.environ["ANTHROPIC_API_KEY"]
    if "ANTHROPIC_AUTH_TOKEN" in os.environ:
        del os.environ["ANTHROPIC_AUTH_TOKEN"]

    cli_path = shutil.which("claude")
    if cli_path is None:
        raise AuthError(
            "`claude` CLI not found on PATH. Install Claude Code first: "
            "https://code.claude.com/docs/en/setup"
        )

    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    creds_file = CREDENTIALS_PATH if CREDENTIALS_PATH.exists() else None

    if token:
        mode = "oauth_token"
    elif creds_file is not None:
        mode = "keychain_login"
    else:
        raise AuthError(
            "No subscription auth available. Either (a) run `claude login` "
            "for an interactive session, or (b) run `claude setup-token` and "
            "paste the value into .env (see .env.example).\n"
            "Without one of these, the SDK has no credentials to use."
        )

    cli_version: str | None = None
    try:
        out = subprocess.run(
            [cli_path, "--version"], capture_output=True, text=True, timeout=10
        )
        if out.returncode == 0:
            cli_version = out.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass

    return AuthStatus(
        auth_mode=mode,
        api_key_scrubbed=api_key_was_set,
        claude_cli_path=cli_path,
        claude_cli_version=cli_version,
        credentials_file=creds_file,
    )
