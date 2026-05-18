"""Click-based CLI: auth-check, run, status, report."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from harness.auth import AuthError, configure_auth
from harness.config import load_config
from harness.orchestrator import CostExceeded, run_pipeline
from harness.state import StateDB

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state.db"
RESULTS_ROOT = REPO_ROOT / "results"

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True,
                              show_path=False, markup=False)],
    )


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="DEBUG logging.")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """vuln-harness — Cloudflare-style 8-stage vulnerability discovery."""
    ctx.ensure_object(dict)
    _setup_logging(verbose)


@main.command("auth-check")
def auth_check() -> None:
    """Verify Claude Code subscription auth is configured correctly."""
    try:
        status = configure_auth()
    except AuthError as e:
        console.print(f"[red]auth error:[/red] {e}")
        sys.exit(2)
    if status.auth_mode == "oauth_token":
        console.print("[green]OK[/green] using CLAUDE_CODE_OAUTH_TOKEN")
    elif status.auth_mode == "keychain_login":
        console.print(
            f"[green]OK[/green] using stored login from {status.credentials_file}"
        )
    if status.api_key_scrubbed:
        console.print("[yellow]scrubbed[/yellow] ANTHROPIC_API_KEY removed from env "
                      "(it would have taken precedence over OAuth)")
    console.print(f"claude CLI: {status.claude_cli_path} ({status.claude_cli_version})")


@main.command("run")
@click.option("--repo", "repo", required=True, type=click.Path(exists=True, file_okay=False),
              help="Path to the target source-code repo.")
@click.option("--run-id", default=None, help="Run identifier (default: random).")
@click.option("--resume", is_flag=True, help="Resume an existing run-id.")
@click.option("--max-cost-usd", default=None, type=float,
              help="Abort if cumulative cost crosses this threshold.")
@click.option("--max-concurrency", default=None, type=int,
              help="Cap every stage's concurrency to this (cost containment).")
@click.option("--max-recon-tasks", default=None, type=int,
              help="Cap the number of initial Hunt tasks Recon may emit.")
@click.option("--config", "config_path", default=None, type=click.Path(),
              help="Override config/stages.yaml.")
def run(repo: str, run_id: str | None, resume: bool, max_cost_usd: float | None,
        max_concurrency: int | None, max_recon_tasks: int | None,
        config_path: str | None) -> None:
    """Run the full 8-stage pipeline against a target repo."""
    try:
        configure_auth()
    except AuthError as e:
        console.print(f"[red]auth error:[/red] {e}")
        sys.exit(2)

    config = load_config(Path(config_path)) if config_path else load_config()
    if max_concurrency is not None:
        config.cap_concurrency(max_concurrency)
        console.print(f"[cyan]capped concurrency to {max_concurrency} across all stages[/cyan]")
    run_id = run_id or f"run_{uuid.uuid4().hex[:8]}"
    repo_path = Path(repo).resolve()

    db = StateDB(DB_PATH)
    try:
        report = asyncio.run(run_pipeline(
            repo_path=repo_path,
            run_id=run_id,
            db=db,
            config=config,
            max_cost_usd=max_cost_usd,
            resume=resume,
            max_recon_tasks=max_recon_tasks,
        ))
        console.print(f"[green]done[/green] run_id={run_id} report={report}")
    except CostExceeded as e:
        console.print(f"[yellow]aborted[/yellow] {e}")
        sys.exit(3)
    except Exception as e:
        console.print(f"[red]failed[/red] {type(e).__name__}: {e}")
        raise
    finally:
        db.close()


@main.command("status")
@click.option("--run-id", default=None)
def status(run_id: str | None) -> None:
    """Show pipeline status: tasks, findings, traces, cost."""
    db = StateDB(DB_PATH)
    try:
        if run_id is None:
            _show_runs_table(db)
            return
        run = db.get_run(run_id)
        if run is None:
            console.print(f"[red]unknown run_id {run_id!r}[/red]")
            sys.exit(1)
        _show_run_detail(db, run_id)
    finally:
        db.close()


@main.command("report")
@click.option("--run-id", required=True)
@click.option("--format", "fmt", type=click.Choice(["json", "md"]), default="json")
def report(run_id: str, fmt: str) -> None:
    """Print (or generate) the final report."""
    db = StateDB(DB_PATH)
    try:
        report_path = RESULTS_ROOT / run_id / "report" / "report.json"
        if not report_path.exists():
            console.print(f"[red]no report at {report_path}[/red]")
            sys.exit(1)
        payload = json.loads(report_path.read_text())
        if fmt == "json":
            click.echo(json.dumps(payload, indent=2))
        else:
            click.echo(_render_markdown_report(payload))
    finally:
        db.close()


def _show_runs_table(db: StateDB) -> None:
    runs = db.list_runs()
    t = Table(title="runs", show_lines=False)
    t.add_column("run_id")
    t.add_column("repo")
    t.add_column("status")
    t.add_column("cost ($)")
    for r in runs:
        t.add_row(r["run_id"], r["repo_path"], r["status"],
                  f"{db.total_cost(r['run_id']):.4f}")
    console.print(t)


def _show_run_detail(db: StateDB, run_id: str) -> None:
    tasks = db.get_all_tasks(run_id)
    findings = db.get_findings(run_id)
    confirmed = [f for f in findings if f.validation_status == "confirmed"]
    canonical = [f for f in confirmed if f.is_canonical]
    reachable = db.get_reachable_canonical_findings(run_id)

    t = Table(title=f"run {run_id}", show_lines=False)
    t.add_column("metric"); t.add_column("count")
    t.add_row("tasks (total)", str(len(tasks)))
    t.add_row("tasks (pending)", str(sum(1 for x in tasks if x.status == "pending")))
    t.add_row("tasks (done)", str(sum(1 for x in tasks if x.status == "done")))
    t.add_row("tasks (failed)", str(sum(1 for x in tasks if x.status == "failed")))
    t.add_row("findings (raw)", str(len(findings)))
    t.add_row("findings (confirmed)", str(len(confirmed)))
    t.add_row("findings (canonical)", str(len(canonical)))
    t.add_row("findings (reachable)", str(len(reachable)))
    t.add_row("total cost ($)", f"{db.total_cost(run_id):.4f}")
    console.print(t)


def _render_markdown_report(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Vulnerability report — `{report['run_id']}`")
    lines.append(f"Target: `{report['target']['repo_path']}`  ")
    s = report["summary"]
    by = s.get("by_severity", {})
    lines.append(f"**Total findings: {s['total']}** — "
                 + ", ".join(f"{k}: {v}" for k, v in by.items()) if by
                 else f"**Total findings: {s['total']}**")
    lines.append("")
    for f in report["findings"]:
        lines.append(f"## {f['title']}")
        lines.append(f"- **Severity**: {f['severity']}  ")
        lines.append(f"- **Class**: {f['vuln_class']}"
                     + (f" ({f['cwe']})" if f.get("cwe") else ""))
        lines.append(f"- **Location**: `{f['file']}:{f['line_start']}-{f['line_end']}`  ")
        lines.append("")
        lines.append(f["description"])
        lines.append("")
        lines.append("```")
        lines.append(f["evidence"])
        lines.append("```")
        lines.append("")
        ep = f["trace"].get("entry_points", [])
        if ep:
            lines.append("**Entry points**:")
            for e in ep:
                lines.append(f"- `{e['kind']}` at `{e['location']}`")
            lines.append("")
        cc = f["trace"].get("call_chain", [])
        if cc:
            lines.append("**Call chain**:")
            for frame in cc:
                lines.append(f"1. `{frame['file']}:{frame['line']}` — `{frame['function']}()`")
            lines.append("")
        lines.append(f"**Recommendation**: {f['recommendation']}")
        lines.append("")
        if f.get("variants"):
            lines.append(f"_Variants_: {', '.join(f['variants'])}")
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
