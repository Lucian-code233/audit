"""Single-stage smoke test: run Recon against ../testtarget via the live
cursor agent, and print what came back. Run inside the activated venv:

    python smoke_recon.py
"""
import asyncio, logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
from audit.runner import run_agent

TARGET = (Path(__file__).resolve().parent.parent / "testtarget").resolve()


async def main():
    res = await run_agent(
        stage="recon",
        prompt_file=Path("prompts/01-recon.md"),
        user_input={"repo_path": str(TARGET), "max_tasks": 3},
        schema_file=Path("schemas/recon_output.schema.json"),
        allowed_tools=["Read", "Grep", "Glob", "Bash"],
        model="composer-2.5",
        cwd=TARGET,
        add_dirs=[TARGET],
        max_turns=30,
        artifact_dir=Path("results/_smoke/recon"),
        artifact_name="recon",
        repair_attempts=1,
    )
    print("\n=== OK ===")
    print("tokens in/out:", res.input_tokens, res.output_tokens)
    print("repair_used:", res.repair_used)
    p = res.payload
    print("subsystems:", len(p.get("subsystems", [])),
          "| initial_tasks:", len(p.get("initial_tasks", [])))
    for t in p.get("initial_tasks", []):
        print("  -", t.get("attack_class"), "→", t.get("scope_hint"))
    print("artifact:", res.artifact_path)


asyncio.run(main())
