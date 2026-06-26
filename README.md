# audit

An 8-stage vulnerability-discovery agent, driven by the local **cursor
`agent` CLI** (Composer and other cursor-hosted models). Many narrow agents,
deliberate disagreement, and an explicit reachability gate.

MIT-licensed. No Anthropic API key needed — auth comes from `agent login`
or `CURSOR_API_KEY`.

## Origin

This project is a from-scratch reimplementation of the pipeline described in
Cloudflare's [Project Glasswing](https://blog.cloudflare.com/cyber-frontier-models/)
post, which tested Anthropic's Mythos preview LLM against Cloudflare's own
codebase. The blog argues that real-world vulnerability discovery does **not**
come from asking one big model "find bugs here" — it comes from:

1. **Many narrow agents** working in parallel on tightly-scoped questions
   ("Look for command injection in this specific function, with this trust
   boundary above it") rather than one exhaustive agent.
2. **Deliberate disagreement** — a second agent, on a different model, that
   tries to *disprove* the first agent's findings.
3. **A reachability trace** as the gating step — most "is this code buggy?"
   findings are noise unless an attacker-controlled input can actually reach
   the sink from outside the system.
4. **A feedback loop** so reachable bugs in one place automatically seed
   hunts for the same pattern elsewhere.

This repo packages that pipeline into a runnable agent. The Cloudflare post
showed the architecture; this codebase ships the prompts, schemas, state
store, and orchestrator.

## The 8 stages

![Vulnerability discovery harness — 8 stages](https://raw.githubusercontent.com/evilsocket/audit/main/docs/pipeline.png)

<sub>Diagram from Cloudflare's [Project Glasswing](https://blog.cloudflare.com/cyber-frontier-models/) post, reproduced here for reference.</sub>

| # | Stage    | Default model | Purpose |
|---|----------|---------------|---------|
| 1 | Recon    | composer-2.5  | Map the repo, emit narrowly-scoped Hunt tasks |
| 2 | Hunt     | composer-2.5 | One attack class per agent; compile/run PoCs |
| 3 | Validate | composer-2.5 | Adversarial re-read; tries to **disprove** Hunt's findings |
| 4 | Gapfill  | composer-2.5 | Re-queue under-covered areas |
| 5 | Dedupe   | composer-2.5 | Cluster findings by root cause |
| 6 | Trace    | composer-2.5 | Prove attacker-controlled input reaches the sink |
| 7 | Feedback | composer-2.5 | Turn reachable traces into new Hunt tasks |
| 8 | Report   | composer-2.5 | Schema-validated structured report |

Each stage is one markdown prompt in `prompts/` + one JSON Schema in
`schemas/`. The orchestrator passes the schema into the system prompt so
every output is shape-stable on the first try.

## Quickstart

```bash
# 1. Install (Python 3.11+)
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Auth (pick one)
#    (a) Already logged in via `agent login`? You're done.
#    (b) Or set an API key for CI / non-interactive use:
echo "CURSOR_API_KEY=<paste>" > .env

# 3. Verify
audit auth-check

# 4. Run
audit run --repo /path/to/target --run-id my-run
audit status --run-id my-run
audit report --run-id my-run --format md > report.md
```

The agent shells out to the local cursor `agent` binary in headless
streaming mode (`agent --print --output-format stream-json`). It must be on
your `PATH`; `audit auth-check` verifies both the binary and a usable
credential.

## Using a different model

Models are configured per-stage in `config/stages.yaml` as cursor model
names. Run `agent --list-models` (while logged in) to see what your account
can use. Examples: `composer-2.5`, `sonnet-4-thinking`, `gpt-5`, or
parameterized forms like `claude-opus-4-8[context=1m,effort=high]`.

The defaults run **every stage on `composer-2.5`**. Note this collapses the
blog's Hunt-vs-Validate "deliberate disagreement" into a prompt-only check
(Validate still adversarially re-reads, just on the same model). To restore
model-level diversity, point Validate (and optionally Trace) at a different
model your account exposes. Edit the YAML to change any stage's model.

Caveats:
- `tools:` in `stages.yaml` only matters for the presence of `Bash`. A
  stage whose tools include `Bash` runs the cursor agent in `--yolo` (full
  shell access); a stage without `Bash` runs in `--mode plan` (read-only).
  cursor has no finer-grained per-tool allowlist, so `Read`/`Grep`/`Glob`
  are advisory hints, not enforced limits.
- Non-Claude / non-Composer models may not produce schema-compliant JSON
  as reliably. The runner's schema-validation + repair turn still applies;
  quality varies by model.
- The cursor CLI returns no per-run cost, so `--max-cost-usd` is ignored
  (see Cost containment).

## Cost containment

A real production codebase can produce 15-50 Hunt tasks and 25+ findings to
validate. At default concurrency this gets expensive. The cursor CLI does
**not** report a per-run dollar cost, so the `--max-cost-usd` budget guard
is disabled (the flag is accepted but ignored). Contain cost with the
structural caps instead:

```bash
audit run --repo /path/to/target \
  --max-concurrency 1 \           # one cursor subprocess at a time
  --max-recon-tasks 15            # cap initial Hunt fanout
```

`--max-concurrency 1` is the biggest lever — it serializes every stage so
at most one `agent` process runs at a time.

## Live-target reproduction (optional)

If the target has a running deployment, point the agents at it. Hunt now
**reproduces** each finding against the live service instead of compiling
a local PoC, Validate **rejects** findings that don't reproduce, and Trace
**confirms** reachability with real HTTP round-trips. The static path
remains available — these flags are opt-in.

```bash
audit run --repo /path/to/target --run-id live \
  --max-concurrency 1 \
  --target-url http://server.local:8888 \
  --target-creds email=admin@system.com \
  --target-creds password=changechangeme
```

Rules the agents follow when `--target-url` is set:
- Network egress is restricted to that host + `127.0.0.1`. No other external
  hosts.
- A finding that doesn't reproduce against the live target is dropped or
  rejected (depending on stage) — "no fabrication".
- Credentials flow into every relevant stage's user_input as a dict.

## Scope notes (optional)

Targets often have intentionally-loose-by-design surfaces that aren't bugs
(e.g. plaintext API keys when that's a feature, test-only Mailpit endpoints,
anonymous-analytics ingest). Drop them in a text file and pass it in — the
notes are appended verbatim to every stage's user_input, and Recon / Hunt /
Validate honor exclusions you list.

```bash
audit run --repo /path/to/target --scope-notes target_scope.md
```

Example `target_scope.md`:

```markdown
- Mailpit (port 1025) is test-only; ignore.
- Plaintext API keys in the database are a required feature.
- Don't flag rate-limit absence on anonymous /ping endpoints.
- Only consider critical/high severity.
```

## Recon mines git history

Recon greps the git history for past security patches
(`CVE`, `sec:`, `fix.*auth`, `sanitize`, …) — patched files are hardened,
but **sibling files with the same idiom often aren't**. Findings get seeded
against the unpatched copies. Adds zero cost on repos without that pattern;
catches real cross-component bugs on repos that have it.

## Logic chains

The pipeline's default is one-attack-class-per-task (the Cloudflare paper's
narrow-scope rule). Recon can also emit `logic_chain` tasks for high-impact
multi-component paths (auth-bypass + IDOR + path-traversal that compose into
RCE, etc.) — one chain per task, with the `scope_hint` naming the specific
chain. This is the one allowed exception to single-attack-class scoping.

## Layout

```
prompts/        8 stage prompts (markdown, prepended to the agent prompt)
schemas/        9 JSON schemas — every agent output is validated
config/         stages.yaml — model + concurrency + tool mode per stage
audit/          Python package
  auth.py       cursor `agent` CLI preflight (binary + credential check)
  state.py      SQLite DAO (runs, tasks, findings, traces, dedupe, costs)
  runner.py     cursor-CLI driver: stream-json parse + schema validation + repair turn
  orchestrator.py pipeline driver
  stages/       one module per stage
work/           per-Hunt-task scratch dirs (sandbox for PoC compile/run)
results/        JSONL artifacts per stage + final report.json
state.db        SQLite (gitignored)
```

## Safety

Hunt agents run with `--yolo` (full shell access) inside per-task scratch
dirs. They are **not** sandboxed at the OS level. Run the audit inside a
disposable VM or container when you don't trust the target source — a
target with malicious build scripts could otherwise execute on your host
during PoC compilation. (`--yolo` is the cursor equivalent of running every
tool call without approval; there is no narrower per-tool gate, so treat
shell-capable stages as fully trusted-to-execute.)

The agent reads the target repo (passed via `--workspace` and named in the
prompt), including any `.env` or `secrets/` directories in it. Outputs land
in `results/<run-id>/` which is `.gitignore`d but **not** scrubbed of those
reads.

## License

[MIT](LICENSE). Reuse freely. No warranty.

## Acknowledgements

- The pipeline design is from Cloudflare's [Project Glasswing](https://blog.cloudflare.com/cyber-frontier-models/)
  blog post. The credit for the architecture goes there.
- Driven by the [cursor agent CLI](https://cursor.com/).
