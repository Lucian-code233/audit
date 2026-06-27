"""Run one agent via the cursor `agent` CLI, send a JSON input, parse +
schema-validate the final JSON output, and persist a JSONL artifact of
every message exchanged.

Drives the cursor binary in headless streaming mode:

    agent --print --output-format stream-json --model <m> \
          --workspace <cwd> [--yolo | --mode plan] --trust "<prompt>"

The CLI emits one JSON object per line on stdout. We consume that stream,
re-serialize each message into our own self-describing `.jsonl` artifact
format (meta/user/assistant/thinking/tool_use/tool_result/result), and
pull the final assistant text out for schema validation.

A schema-validation failure is followed up with a repair turn. Because the
cursor CLI is one-shot per process, the repair turn resumes the same chat
session via `--resume <session_id>` so the model keeps its context.

API-error handling: the cursor CLI surfaces overloaded / quota-exhausted
errors either as a `result` line with `is_error=true` or as a non-zero
process exit. We detect this BEFORE schema validation, classify the error,
and either retry with exponential backoff (transient) or raise
QuotaExhaustedError (terminal).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audit.json_utils import extract_json, validate_schema

log = logging.getLogger(__name__)

# Tools that imply the agent needs to run shell commands. Every stage now
# runs under --yolo (cursor has no per-tool enforcement), so this set only
# drives the wording of the in-prompt tool policy: stages without a shell
# tool are told they may not run shell / mutate files at all.
_SHELL_TOOLS = {"Bash"}


@dataclass
class AgentResult:
    payload: dict
    cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_tokens: int | None
    num_turns: int | None
    duration_ms: int | None
    session_id: str | None
    artifact_path: Path
    repair_used: bool
    raw_result_message: dict = field(default_factory=dict)


class AgentRunError(RuntimeError):
    """Schema validation failed after repair attempts (model produced
    parseable output that didn't match the schema)."""


class TransientAgentError(RuntimeError):
    """API returned a transient error (overloaded, generic 5xx).
    The agent call should be retried with backoff."""


class QuotaExhaustedError(RuntimeError):
    """The subscription has run out of quota. Don't retry — abort the
    pipeline and let the user wait for the reset window."""


class AgentTimeout(RuntimeError):
    """The agent subprocess exceeded its wall-clock budget and was killed.
    Deliberately NOT a TransientAgentError: a hung hunter won't un-hang on
    retry, so retrying would just burn another full timeout. Propagates to
    the stage's generic handler, which marks the task 'failed' and moves on
    — we'd rather drop a stuck task than stall the whole pipeline."""


_QUOTA_MARKERS = (
    "out of extra usage",
    "usage limit reached",
    # Subscription session/usage caps that reset on a timer, e.g.
    # "You've hit your session limit · resets 5:10am (UTC)". The reset is
    # often hours out, so backoff-retrying is futile — treat it as terminal
    # and let the caller abort into a resumable state.
    "session limit",
    "your plan has no remaining",
)

_TRANSIENT_MARKERS = (
    "api error: 529",
    "overloaded",
    "api error: 503",
    "api error: 502",
    "api error: 504",
    "api error: 500",
    "rate_limit",
    "temporarily unavailable",
    "service unavailable",
)


def _classify_api_error(text: str) -> tuple[str, type[RuntimeError]]:
    """Return (label, exception_class) for an is_error response."""
    t = (text or "").lower()
    if any(m in t for m in _QUOTA_MARKERS):
        return "quota_exhausted", QuotaExhaustedError
    if any(m in t for m in _TRANSIENT_MARKERS):
        return "transient", TransientAgentError
    # Default to transient — better to retry once than abort on classification miss.
    return "unknown_api_error", TransientAgentError


async def run_agent(
    *,
    stage: str,
    prompt_file: Path,
    user_input: dict,
    schema_file: Path,
    allowed_tools: list[str],
    model: str,
    cwd: Path,
    add_dirs: list[Path] | None = None,
    max_turns: int = 25,
    permission_mode: str = "acceptEdits",
    artifact_dir: Path,
    artifact_name: str,
    repair_attempts: int = 1,
    transient_retries: int = 3,
    transient_base_delay: float = 30.0,
    timeout_s: float | None = 1200.0,
) -> AgentResult:
    """Run one agent, retrying transient API errors with exponential backoff.

    Raises `QuotaExhaustedError` if the subscription is out of quota
    (caller should abort the run). Raises `TransientAgentError` if all
    backoff retries are exhausted. Raises `AgentRunError` if the model
    produced parseable output that doesn't match the schema even after
    repair turns. Raises `AgentTimeout` if a single agent attempt exceeds
    `timeout_s` wall-clock (the subprocess is killed; not retried).
    """
    last_exc: RuntimeError | None = None
    for attempt in range(transient_retries + 1):
        try:
            return await _run_agent_once(
                stage=stage,
                prompt_file=prompt_file,
                user_input=user_input,
                schema_file=schema_file,
                allowed_tools=allowed_tools,
                model=model,
                cwd=cwd,
                add_dirs=add_dirs,
                max_turns=max_turns,
                permission_mode=permission_mode,
                artifact_dir=artifact_dir,
                artifact_name=artifact_name,
                repair_attempts=repair_attempts,
                timeout_s=timeout_s,
            )
        except QuotaExhaustedError:
            raise
        except AgentTimeout:
            # A hung subprocess won't recover on retry — fail fast.
            raise
        except TransientAgentError as e:
            last_exc = e
            if attempt >= transient_retries:
                break
            delay = min(transient_base_delay * (2 ** attempt), 240.0)
            log.warning(
                "[%s/%s] transient API error (attempt %d/%d): %s — retrying in %.0fs",
                stage, artifact_name, attempt + 1, transient_retries + 1,
                str(e)[:160], delay,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _build_system_prompt(prompt_file: Path, schema_file: Path) -> str:
    """The stage prompt with the literal output schema appended, so the
    model never has to guess field names. cursor has no --system-prompt
    flag, so this is prepended to the user prompt instead."""
    system_prompt = prompt_file.read_text()
    schema_text = schema_file.read_text()
    system_prompt += (
        "\n\n# Output schema\n\n"
        "Your output MUST validate against this JSON Schema. "
        "Pay attention to nested objects, required fields, and "
        "`additionalProperties: false`.\n\n"
        f"```json\n{schema_text}\n```\n"
    )
    return system_prompt


def _cursor_cmd(
    *,
    model: str,
    cwd: Path,
    allowed_tools: list[str],
    resume_session: str | None,
) -> list[str]:
    """Assemble the cursor CLI argv (minus the trailing prompt)."""
    agent_bin = shutil.which("agent") or "agent"
    cmd = [
        agent_bin,
        "--print",
        "--output-format", "stream-json",
        "--model", model,
        "--workspace", str(cwd),
        "--trust",
        "--yolo",
    ]
    if resume_session:
        cmd += ["--resume", resume_session]
    return cmd


def _tool_policy_note(allowed_tools: list[str]) -> str:
    """A prompt block that states the tool allowlist explicitly.

    Every stage runs under --yolo (cursor has no per-tool enforcement), so
    we restate the allowlist in the prompt and forbid everything else —
    most importantly any file mutation. This is advisory, not a hard
    sandbox; OS-level isolation remains the real boundary."""
    allowed = ", ".join(allowed_tools) if allowed_tools else "(none)"
    can_shell = bool(_SHELL_TOOLS.intersection(allowed_tools))
    forbidden = (
        "Do NOT modify, create, or delete any files; do NOT run shell "
        "commands; do NOT make network requests."
        if not can_shell else
        "Use the shell ONLY to READ and INSPECT (e.g. cat, ls, grep, find, "
        "compiling/running a self-contained PoC inside your scratch dir). "
        "Do NOT modify files outside your scratch dir, exfiltrate data, or "
        "make external network requests."
    )
    return (
        "\n\n# Tool policy (strict)\n\n"
        f"You may use ONLY these tools for this task: {allowed}. "
        f"{forbidden}\n"
    )


async def _run_agent_once(
    *,
    stage: str,
    prompt_file: Path,
    user_input: dict,
    schema_file: Path,
    allowed_tools: list[str],
    model: str,
    cwd: Path,
    add_dirs: list[Path] | None,
    max_turns: int,
    permission_mode: str,
    artifact_dir: Path,
    artifact_name: str,
    repair_attempts: int,
    timeout_s: float | None = 1200.0,
) -> AgentResult:
    """Single attempt. Raises TransientAgentError / QuotaExhaustedError
    before schema validation if the CLI returned an error result."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{artifact_name}.jsonl"
    cwd.mkdir(parents=True, exist_ok=True)

    system_prompt = _build_system_prompt(prompt_file, schema_file)
    # The agent's cwd is --workspace; any extra readable roots (e.g. the
    # target repo when running inside a scratch dir) are named in the
    # prompt so the agent can reach them by absolute path under --yolo.
    add_dir_note = ""
    if add_dirs:
        roots = ", ".join(str(p) for p in add_dirs)
        add_dir_note = (
            f"\n\n# Accessible paths\n\nYou may read these absolute paths "
            f"in addition to your workspace: {roots}\n"
        )
    initial_prompt = (
        system_prompt + add_dir_note + _tool_policy_note(allowed_tools)
        + "\n\n# Task input\n\n```json\n"
        + json.dumps(user_input, ensure_ascii=False)
        + "\n```\n"
    )

    last_text = ""
    last_result_msg: dict[str, Any] = {}
    session_id: str | None = None
    repair_used = False

    with artifact_path.open("w") as art:
        _write_artifact(art, {"kind": "meta", "stage": stage, "model": model, "started_at": time.time()})
        _write_artifact(art, {"kind": "user", "text": initial_prompt[:50000]})

        last_text, last_result_msg, session_id = await _run_cursor(
            stage=stage,
            artifact_name=artifact_name,
            cmd=_cursor_cmd(model=model, cwd=cwd, allowed_tools=allowed_tools,
                            resume_session=None),
            prompt=initial_prompt,
            art=art,
            timeout_s=timeout_s,
        )

        # Before schema validation: was this a real model response, or did
        # the CLI surface an API error in the result line?
        if last_result_msg.get("is_error"):
            label, exc_cls = _classify_api_error(last_text)
            _write_artifact(art, {"kind": "api_error", "classification": label,
                                  "text": last_text[:1000]})
            raise exc_cls(
                f"[{stage}/{artifact_name}] {label}: "
                f"{(last_text or '').strip()[:300]}"
            )

        attempts = 0
        errors = _validate(last_text, schema_file)
        while errors and attempts < repair_attempts:
            attempts += 1
            repair_used = True
            repair_prompt = _build_repair_prompt(last_text, errors, schema_file)
            _write_artifact(art, {"kind": "repair_request", "text": repair_prompt[:50000]})
            # Resume the same chat so the model keeps its context. If we never
            # got a session_id back, fall back to a stateless repair prompt.
            last_text, last_result_msg, session_id = await _run_cursor(
                stage=stage,
                artifact_name=artifact_name,
                cmd=_cursor_cmd(model=model, cwd=cwd, allowed_tools=allowed_tools,
                                resume_session=session_id),
                prompt=repair_prompt,
                art=art,
                timeout_s=timeout_s,
            )
            if last_result_msg.get("is_error"):
                label, exc_cls = _classify_api_error(last_text)
                _write_artifact(art, {"kind": "api_error_on_repair",
                                      "classification": label,
                                      "text": last_text[:1000]})
                raise exc_cls(
                    f"[{stage}/{artifact_name}] {label} on repair turn: "
                    f"{(last_text or '').strip()[:300]}"
                )
            errors = _validate(last_text, schema_file)

        if errors:
            _write_artifact(art, {"kind": "schema_errors", "errors": errors})
            raise AgentRunError(
                f"[{stage}/{artifact_name}] schema validation failed after "
                f"{repair_attempts} repair attempts: {errors[:5]}"
            )

        payload = extract_json(last_text)
        _write_artifact(art, {"kind": "final_payload", "payload": payload})

    usage = last_result_msg.get("usage") or {}
    return AgentResult(
        payload=payload,
        cost_usd=last_result_msg.get("total_cost_usd"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_tokens=usage.get("cache_read_input_tokens"),
        cache_creation_tokens=usage.get("cache_creation_input_tokens"),
        num_turns=last_result_msg.get("num_turns"),
        duration_ms=last_result_msg.get("duration_ms"),
        session_id=last_result_msg.get("session_id") or session_id,
        artifact_path=artifact_path,
        repair_used=repair_used,
        raw_result_message=last_result_msg,
    )


async def _run_cursor(
    *,
    stage: str,
    artifact_name: str,
    cmd: list[str],
    prompt: str,
    art,
    timeout_s: float | None = 1200.0,
) -> tuple[str, dict[str, Any], str | None]:
    """Spawn the cursor CLI, stream-parse stdout, write each message to the
    JSONL artifact, and return (final assistant text, result_dict, session_id).

    Raises TransientAgentError if the process can't be started or dies
    without producing a result line (treated as retry-worthy). Raises
    AgentTimeout if the subprocess produces no result within `timeout_s`
    wall-clock — the process is killed and the task is failed, not retried."""
    full_cmd = [*cmd, prompt]
    try:
        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # The cursor CLI emits one JSON object per line. A single line
            # (assistant message / result with large diffs or file contents)
            # routinely exceeds asyncio's default 64 KiB StreamReader buffer,
            # which makes `async for raw in proc.stdout` raise LimitOverrunError
            # ("Separator is/ is not found, ... chunk ... limit"). Bump to 32 MiB.
            limit=32 * 1024 * 1024,
        )
    except (OSError, ValueError) as e:
        raise TransientAgentError(
            f"[{stage}/{artifact_name}] failed to spawn cursor agent: {e}"
        ) from e

    last_assistant_text = ""
    thinking_buf: list[str] = []
    result_msg: dict[str, Any] = {}
    session_id: str | None = None
    saw_result = False

    async def _consume() -> tuple[str, dict[str, Any], str | None]:
        nonlocal last_assistant_text, result_msg, session_id, saw_result
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Non-JSON noise on stdout (e.g. an auth/usage error printed
                # outside the stream). Capture it so the error classifier and
                # artifact have something to work with.
                _write_artifact(art, {"kind": "stdout_nonjson", "text": line[:2000]})
                if not result_msg:
                    result_msg = {"is_error": True, "result": line}
                    saw_result = True
                continue

            norm = _normalize_cursor_msg(obj)
            if norm is not None:
                _write_artifact(art, norm)

            sid = obj.get("session_id")
            if sid:
                session_id = sid

            mtype = obj.get("type")
            if mtype == "thinking" and obj.get("subtype") == "delta":
                thinking_buf.append(obj.get("text", ""))
            elif mtype == "thinking" and obj.get("subtype") == "completed":
                # Flush the buffered reasoning as one artifact line. cursor
                # streams thinking as deltas with the full text only
                # reconstructable by concatenation; without this the trace
                # loses the model's reasoning entirely.
                text = "".join(thinking_buf)
                thinking_buf.clear()
                if text:
                    _write_artifact(art, {"kind": "thinking", "text": text})
            elif mtype == "assistant":
                text = _assistant_text(obj)
                if text:
                    last_assistant_text = text
            elif mtype == "result":
                result_msg = _result_to_dict(obj)
                saw_result = True

        stderr_bytes = await proc.stderr.read() if proc.stderr else b""
        await proc.wait()
        stderr_text = stderr_bytes.decode("utf-8", "replace").strip()

        if not saw_result:
            # No result line at all → treat the process exit + stderr as the
            # error surface, classified downstream.
            msg = stderr_text or f"cursor agent exited {proc.returncode} with no result"
            _write_artifact(art, {"kind": "no_result", "returncode": proc.returncode,
                                  "stderr": stderr_text[:2000]})
            result_msg = {"is_error": True, "result": msg}
            return msg, result_msg, session_id

        # Prefer the result line's full text (== final assistant message);
        # fall back to the last streamed assistant block.
        final_text = result_msg.get("result") or last_assistant_text
        return final_text, result_msg, session_id

    try:
        return await asyncio.wait_for(_consume(), timeout=timeout_s)
    except (asyncio.TimeoutError, TimeoutError):
        # The agent hung (stuck PoC, dead network read, infinite loop). Kill
        # the subprocess so it can't leak as an orphan, then fail the task.
        await _kill_proc(proc)
        _write_artifact(art, {"kind": "timeout", "timeout_s": timeout_s,
                              "pid": proc.pid})
        raise AgentTimeout(
            f"[{stage}/{artifact_name}] agent exceeded {timeout_s:.0f}s "
            f"wall-clock budget — killed pid {proc.pid}"
        )


async def _kill_proc(proc: asyncio.subprocess.Process) -> None:
    """Best-effort terminate→kill of a subprocess and reap it, so a hung
    agent never lingers as an orphan holding scratch files / sockets."""
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=10.0)
    except (asyncio.TimeoutError, TimeoutError):
        log.warning("subprocess pid %s did not exit after kill", proc.pid)


def _assistant_text(obj: dict[str, Any]) -> str:
    """Concatenate text blocks from a cursor `assistant` message."""
    content = (obj.get("message") or {}).get("content") or []
    parts = [b.get("text", "") for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    return "".join(parts)


def _normalize_cursor_msg(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Map a raw cursor stream-json line to our self-describing artifact
    format. Returns None for lines we don't persist verbatim (thinking
    deltas are buffered and not individually written)."""
    mtype = obj.get("type")
    sub = obj.get("subtype")

    if mtype == "system" and sub == "init":
        return {"kind": "system_init", "model": obj.get("model"),
                "cwd": obj.get("cwd"), "permission_mode": obj.get("permissionMode")}
    if mtype == "thinking":
        # Thinking is handled statefully in _consume: deltas are buffered
        # and flushed as one {"kind":"thinking"} line on `completed`.
        return None
    if mtype == "assistant":
        return {"kind": "assistant", "content": [{"type": "text",
                "text": _assistant_text(obj)}]}
    if mtype == "tool_call":
        return _normalize_tool_call(obj)
    if mtype == "result":
        return {"kind": "result", **_result_to_dict(obj)}
    # Pass through anything else (e.g. "user" echo) with a generic kind.
    if mtype == "user":
        return {"kind": "user_echo"}
    return {"kind": f"cursor_{mtype}", "subtype": sub}


def _normalize_tool_call(obj: dict[str, Any]) -> dict[str, Any]:
    """Map cursor tool_call/{started,completed} → tool_use / tool_result.

    cursor nests the call under tool_call.<kind>ToolCall (e.g.
    shellToolCall, readToolCall). We extract a name, the input args, and —
    on completion — the result with an is_error flag."""
    sub = obj.get("subtype")
    call_id = obj.get("call_id")
    tc = obj.get("tool_call") or {}
    # tool_call holds exactly one *ToolCall key (shellToolCall, readToolCall…)
    inner_key = next((k for k in tc if k.endswith("ToolCall")), None)
    inner = tc.get(inner_key) or {} if inner_key else {}
    name = inner_key[:-len("ToolCall")] if inner_key else "tool"

    if sub == "started":
        return {
            "kind": "tool_use",
            "id": call_id,
            "name": name,
            "input": inner.get("args") or {},
        }
    # completed → tool_result
    result = inner.get("result") or tc.get("result") or {}
    success = result.get("success") if isinstance(result, dict) else None
    is_error = True
    content: Any = result
    if isinstance(success, dict):
        is_error = success.get("exitCode", 0) not in (0, None)
        content = success.get("stdout", "") or success.get("output", "")
        stderr = success.get("stderr")
        if stderr:
            content = f"{content}\n[stderr]\n{stderr}" if content else stderr
    elif result == {} or result is None:
        is_error = False
        content = ""
    return {
        "kind": "tool_result",
        "tool_use_id": call_id,
        "name": name,
        "content": content,
        "is_error": is_error,
    }


def _result_to_dict(obj: dict[str, Any]) -> dict[str, Any]:
    """Normalize a cursor `result` line into the shape the rest of the
    harness expects. cursor uses camelCase token names and provides no
    cost / num_turns / stop_reason — those are set to None."""
    raw_usage = obj.get("usage") or {}
    usage = {
        "input_tokens": raw_usage.get("inputTokens"),
        "output_tokens": raw_usage.get("outputTokens"),
        "cache_read_input_tokens": raw_usage.get("cacheReadTokens"),
        "cache_creation_input_tokens": raw_usage.get("cacheWriteTokens"),
    }
    return {
        "subtype": obj.get("subtype"),
        "is_error": bool(obj.get("is_error")),
        "duration_ms": obj.get("duration_ms"),
        "duration_api_ms": obj.get("duration_api_ms"),
        "num_turns": None,
        "session_id": obj.get("session_id"),
        "stop_reason": None,
        "total_cost_usd": None,
        "usage": usage,
        "result": obj.get("result"),
        "request_id": obj.get("request_id"),
        "model_usage": None,
    }


def _validate(text: str, schema_file: Path) -> list[str]:
    try:
        payload = extract_json(text)
    except ValueError as e:
        return [f"json_extract: {e}"]
    return validate_schema(payload, schema_file)


def _build_repair_prompt(prev_output: str, errors: list[str], schema_file: Path) -> str:
    err_block = "\n".join(f"- {e}" for e in errors[:20])
    return (
        "Your previous output failed schema validation against "
        f"`{schema_file.name}`. Errors:\n"
        f"{err_block}\n\n"
        "Re-emit the same response, fixing ONLY these errors. Output a "
        "single JSON object — no prose, no markdown fence."
    )


def _write_artifact(fp, obj: Any) -> None:
    fp.write(json.dumps(obj, default=_json_fallback, ensure_ascii=False) + "\n")
    fp.flush()


def _json_fallback(o: Any) -> Any:
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    if isinstance(o, Path):
        return str(o)
    return repr(o)
