"""Run the bundled production pipeline using explicit user-owned paths."""

import argparse
import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from common import RUNTIME, child_env, initialize, save_state, settings


def execute(workspace, options):
    if not settings(workspace).get("DASHSCOPE_API_KEY"):
        raise ValueError("API key is missing; follow references/setup-and-api-key.md")
    for name in ("companies", "tracks"):
        if not Path(options[name]).is_file():
            raise ValueError("Input file not found: " + name)
    if options["companies"] == options["output"]:
        raise ValueError("Output must differ from input workbook")
    command = [
        sys.executable,
        str(RUNTIME / "scripts/run_company_track_pipeline.py"),
        "--companies",
        options["companies"],
        "--tracks",
        options["tracks"],
        "--output",
        options["output"],
        "--work-dir",
        str(workspace / "work"),
        "--env",
        str(workspace / ".env"),
        "--concurrency",
        str(options["concurrency"]),
        "--candidate-budget",
        str(options["candidate_budget"]),
        "--core-budget",
        str(options["core_budget"]),
    ]
    if options.get("sheet"):
        command += ["--sheet", options["sheet"]]
    if options["advanced_search"]:
        command.append("--advanced-search")
    state = {"status": "running", "options": options, "started_at": datetime.now(UTC).isoformat()}
    save_state(workspace, state)
    key = settings(workspace)["DASHSCOPE_API_KEY"]
    child = None
    try:
        child = subprocess.Popen(
            command,
            cwd=workspace,
            env=child_env(workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name != "nt",
        )
        for line in child.stdout:
            print(line.replace(key, "[REDACTED]"), end="", flush=True)
            if line.startswith("[") and "/" in line:
                state["last_progress"] = line.strip()
                save_state(workspace, state)
        status = child.wait()
        state["status"] = "completed" if status == 0 else "failed"
        state["exit_code"] = status
        return status
    except BaseException:
        state["status"] = "interrupted"
        if child and child.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                os.killpg(child.pid, signal.SIGTERM)
            child.wait()
        raise
    finally:
        state["finished_at"] = datetime.now(UTC).isoformat()
        save_state(workspace, state)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "resume", "status"):
        sub = commands.add_parser(name)
        sub.add_argument("--workspace", type=Path, required=True)
        if name == "run":
            sub.add_argument("--companies", type=Path, required=True)
            sub.add_argument("--tracks", type=Path, required=True)
            sub.add_argument("--output", type=Path)
            sub.add_argument("--sheet")
            sub.add_argument("--concurrency", type=int, choices=range(1, 9), default=8)
            sub.add_argument("--candidate-budget", type=int, default=2000)
            sub.add_argument("--core-budget", type=int, default=1600)
            sub.add_argument("--advanced-search", action="store_true")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    initialize(workspace)
    path = workspace / ".run-state.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if args.command == "status":
        print(json.dumps(state or {"status": "not_started"}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "resume":
        if "options" not in state:
            raise ValueError("No saved run exists")
        if state["status"] == "completed" and Path(state["options"]["output"]).is_file():
            print(
                json.dumps(
                    {"status": "completed", "output": state["options"]["output"]},
                    ensure_ascii=False,
                )
            )
            return 0
        return execute(workspace, state["options"])
    if not 1 <= args.core_budget <= args.candidate_budget:
        raise ValueError("Budgets must satisfy 1 <= core_budget <= candidate_budget")
    output = (
        args.output.resolve()
        if args.output
        else workspace / "outputs" / (args.companies.stem + "_赛道判断.xlsx")
    )
    options = {
        "companies": str(args.companies.resolve()),
        "tracks": str(args.tracks.resolve()),
        "output": str(output),
        "sheet": args.sheet,
        "concurrency": args.concurrency,
        "candidate_budget": args.candidate_budget,
        "core_budget": args.core_budget,
        "advanced_search": args.advanced_search,
    }
    previous = dict(state.get("options") or {})
    previous["concurrency"] = options["concurrency"]
    if state.get("options") and previous != options:
        raise ValueError("Workspace belongs to different inputs/options; use a new workspace")
    if output.exists() and state.get("options") != options:
        raise ValueError("Output already exists; select another output path")
    return execute(workspace, options)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
