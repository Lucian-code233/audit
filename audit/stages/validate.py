"""Stage 3: Validate — adversarial review, different model from Hunt."""

from __future__ import annotations

import asyncio
import logging

from audit.runner import AgentRunError, run_agent
from audit.state import Finding, StateDB
from audit.stages._common import StageContext

log = logging.getLogger(__name__)


async def run_validate(ctx: StageContext, db: StateDB) -> int:
    """Validate every finding that hasn't been validated yet. Returns
    count of confirmed findings."""
    unvalidated = db.get_unvalidated_findings(ctx.run_id)
    if not unvalidated:
        log.info("[%s] validate: nothing to validate", ctx.run_id)
        return 0

    sc = ctx.stage("validate")
    sem = asyncio.Semaphore(sc.concurrency)

    log.info(
        "[%s] validate: %d findings (concurrency=%d, model=%s)",
        ctx.run_id, len(unvalidated), sc.concurrency, sc.model,
    )

    tasks_by_id = {t.task_id: t for t in db.get_all_tasks(ctx.run_id)}
    counters = {"confirmed": 0, "rejected": 0, "needs_more_info": 0, "failed": 0}

    async def _one(f: Finding) -> None:
        async with sem:
            task = tasks_by_id.get(f.task_id)
            ctx_block = {
                "attack_class": task.attack_class if task else f.vuln_class,
                "scope_hint": task.scope_hint if task else "",
                "rationale": task.rationale if task else "",
            }
            user_input = {
                "finding": f.raw_json,
                "task_context": ctx_block,
                "repo_path": str(ctx.repo_path),
            }
            try:
                result = await run_agent(
                    stage="validate",
                    prompt_file=ctx.prompt("03-validate"),
                    user_input=user_input,
                    schema_file=ctx.schema("validation"),
                    allowed_tools=sc.tools,
                    model=sc.model,
                    cwd=ctx.repo_path,
                    add_dirs=[ctx.repo_path],
                    max_turns=sc.max_turns,
                    permission_mode=sc.permission_mode,
                    artifact_dir=ctx.results_dir("validate"),
                    artifact_name=f.finding_id,
                    repair_attempts=sc.repair_attempts,
                )
            except AgentRunError as e:
                log.warning("[%s] validate %s failed: %s", ctx.run_id, f.finding_id, e)
                counters["failed"] += 1
                # Treat unparseable validation as needs_more_info to avoid
                # silently confirming.
                db.set_finding_validation(
                    f.finding_id, "needs_more_info",
                    {"finding_id": f.finding_id, "verdict": "needs_more_info",
                     "rationale": f"validator failed to produce schema-valid output: {e}",
                     "validator_confidence": 0.0},
                )
                return

            verdict = result.payload.get("verdict", "needs_more_info")
            db.set_finding_validation(f.finding_id, verdict, result.payload)
            db.record_cost(ctx.run_id, "validate", f.finding_id, result.raw_result_message)
            db.add_artifact(ctx.run_id, "validate", f.finding_id, "jsonl",
                            str(result.artifact_path))
            counters[verdict] = counters.get(verdict, 0) + 1

    await asyncio.gather(*(_one(f) for f in unvalidated))
    log.info(
        "[%s] validate: confirmed=%d rejected=%d needs_more_info=%d failed=%d",
        ctx.run_id,
        counters.get("confirmed", 0),
        counters.get("rejected", 0),
        counters.get("needs_more_info", 0),
        counters["failed"],
    )
    return counters.get("confirmed", 0)
