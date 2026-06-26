"""Auth preflight for the cursor `agent` CLI.

Unlike the Claude Code SDK (which juggles ANTHROPIC_* env vars and OAuth
precedence), the cursor agent has a simple auth surface:

  - `CURSOR_API_KEY` / `CURSOR_AUTH_TOKEN` in the environment, or
  - a stored interactive login from `agent login`.

This module verifies the `agent` binary is on PATH and that *some* usable
credential is present, then reports which mode was detected. It does no env
scrubbing — there are no competing Anthropic vars to outrank.

Modes:
  - **api_key**:  CURSOR_API_KEY / CURSOR_AUTH_TOKEN set in the env.
  - **cli_login**: `agent status` reports an authenticated session (from
    a prior `agent login`).

Anything else raises AuthError.
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
    auth_mode: str            # "api_key" | "cli_login"
    agent_cli_path: str | None
    agent_cli_version: str | None
    api_key_source: str | None  # "CURSOR_API_KEY" | "CURSOR_AUTH_TOKEN" | None


class AuthError(RuntimeError):
    pass


_API_KEY_VARS = ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN")


def _agent_status_ok(agent_path: str) -> bool:
    """Return True if `agent status` reports an authenticated session."""
    try:
        out = subprocess.run(
            [agent_path, "status"], capture_output=True, text=True, timeout=15
        )
    except (subprocess.SubprocessError, OSError):
        return False
    blob = f"{out.stdout}\n{out.stderr}".lower()
    if out.returncode != 0:
        return False
    # `agent status` prints "Not logged in" when unauthenticated.
    if "not logged in" in blob or "authentication required" in blob:
        return False
    return True


def configure_auth(
    env_file: Path | None = None,
    *,
    allow_api_key: bool = True,
) -> AuthStatus:
    """Load .env, verify the cursor agent CLI + a usable credential.

    Args:
        env_file: Optional .env file to load before reading env vars.
        allow_api_key: Accepted for CLI compatibility; cursor always honors
            CURSOR_API_KEY, so this is effectively always-on (kept so the
            existing --allow-api-key flag wiring doesn't break).

    Returns an AuthStatus describing what was detected. Raises AuthError if
    no usable auth path is available.
    """
    if env_file is not None and env_file.exists():
        load_dotenv(env_file)
    else:
        load_dotenv()

    cli_path = shutil.which("agent")
    if cli_path is None:
        raise AuthError(
            "`agent` (cursor) CLI not found on PATH. Install Cursor's CLI "
            "first, then run `agent login`."
        )

    api_key_source: str | None = None
    for var in _API_KEY_VARS:
        if os.environ.get(var, "").strip():
            api_key_source = var
            break

    if api_key_source is not None:
        mode = "api_key"
    elif _agent_status_ok(cli_path):
        mode = "cli_login"
    else:
        raise AuthError(
            "No cursor auth available. Pick one of:\n"
            "  (a) Interactive login: run `agent login`.\n"
            "  (b) Headless: set CURSOR_API_KEY (or CURSOR_AUTH_TOKEN) in the "
            "env or .env file."
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
        agent_cli_path=cli_path,
        agent_cli_version=cli_version,
        api_key_source=api_key_source,
    )
