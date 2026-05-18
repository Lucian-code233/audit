# vuln-harness

Cloudflare-style 8-stage vulnerability discovery harness, driven by your
**Claude Pro / Max subscription** via the official Claude Code Agent SDK.

Implements the pipeline described in Cloudflare's
[Project Glasswing](https://blog.cloudflare.com/cyber-frontier-models/)
post: **Recon → Hunt → Validate → Gapfill → Dedupe → Trace → Feedback →
Report**, with many narrowly-scoped concurrent agents and a different
model for Validate (deliberate disagreement).

## Quickstart

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Generate a 1-year OAuth token bound to your Claude subscription
claude setup-token
# copy the printed token

# 3. Configure
cp .env.example .env
$EDITOR .env                   # paste CLAUDE_CODE_OAUTH_TOKEN
unset ANTHROPIC_API_KEY        # API key takes precedence over OAuth — clear it

# 4. Verify auth
vuln-harness auth-check

# 5. Run against a target repo
vuln-harness run --repo /path/to/target --run-id demo
vuln-harness report --run-id demo --format md > report.md
```

## How it works

The orchestrator drives 8 stages of narrowly-scoped Claude agents:

| # | Stage    | Model | Concurrency | Purpose |
|---|----------|-------|-------------|---------|
| 1 | Recon    | Opus  | 1           | Map the repo, emit initial attack-class tasks |
| 2 | Hunt     | Sonnet| 50          | One attack class per agent; compile/run PoCs |
| 3 | Validate | Opus  | 10          | Adversarial re-read; tries to **disprove** |
| 4 | Gapfill  | Sonnet| 1           | Re-queue under-covered areas back to Hunt |
| 5 | Dedupe   | Sonnet| 1           | Cluster findings by root cause |
| 6 | Trace    | Opus  | 10          | Prove attacker-controlled input reaches the sink |
| 7 | Feedback | Sonnet| 1           | Turn reachable traces into new Hunt tasks |
| 8 | Report   | Sonnet| 1           | Schema-validated structured report |

State lives in `state.db` (SQLite); raw agent outputs in `results/*.jsonl`;
Hunt scratch dirs in `work/hunt/<task_id>/`.

## Authentication

This harness uses **OAuth subscription auth**, not the metered API. The
Agent SDK wraps the `claude` CLI which honors `CLAUDE_CODE_OAUTH_TOKEN`.

**Important precedence rule**: if `ANTHROPIC_API_KEY` is set, it wins
over OAuth and silently bills the API. `harness/auth.py` scrubs it from
the process environment on startup, but you should also `unset` it in
your shell if it leaks from other tooling.

After 2026-06-15, Agent SDK calls on Pro/Max bill against a separate
Agent SDK credit pool — same OAuth token, no code change.

## Safety

Hunt runs Bash to compile and run PoCs in per-task scratch dirs. The
scratch dirs are NOT sandboxed by the harness. **Run inside a disposable
VM or container** if you don't trust the target source.

## Layout

```
prompts/        8 stage prompts (markdown, used as system prompts)
schemas/        9 JSON schemas (validate every agent output)
config/         stages.yaml — model + concurrency + tools per stage
harness/        Python package
  auth.py       OAuth check + ANTHROPIC_API_KEY scrubbing
  state.py      SQLite DAO
  runner.py     claude-agent-sdk wrapper
  orchestrator.py pipeline driver
  stages/       one module per stage
work/           per-task scratch dirs
results/        JSONL artifacts per stage + final report
state.db        SQLite (gitignored)
```

## CLI

```
vuln-harness auth-check
vuln-harness run --repo PATH [--run-id NAME] [--resume] [--max-cost-usd N]
vuln-harness status [--run-id NAME]
vuln-harness report --run-id NAME [--format json|md]
```
