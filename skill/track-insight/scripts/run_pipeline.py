#!/usr/bin/env python3
"""High-level entrypoint for the bundled automated pipeline."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = SKILL_ROOT / "runtime"
NETWORK_ENDPOINTS = (
    ("dashscope.aliyuncs.com", 443, "bailian_api"),
    ("www.gov.cn", 443, "public_pages"),
)


def copy_tree_missing(source: Path, destination: Path) -> None:
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def initialize(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for name in ("data", "logs", "reports"):
        (workspace / name).mkdir(exist_ok=True)
    copy_tree_missing(RUNTIME_ROOT / "config", workspace / "config")
    copy_tree_missing(RUNTIME_ROOT / "docs", workspace / "docs")
    env_path = workspace / ".env"
    if not env_path.exists():
        env_path.write_text(
            "TRACK_INSIGHT_DATABASE_URL=sqlite:///data/track_insight.sqlite3\n"
            "DASHSCOPE_API_KEY=\n"
            "BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\n"
            "TRACK_INSIGHT_LOG_FILE=logs/track-insight.jsonl\n",
            encoding="utf-8",
        )
    ignore_path = workspace / ".gitignore"
    existing = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
    additions = [
        entry
        for entry in (".env", ".python-deps/", "data/", "logs/", "reports/")
        if entry not in existing.splitlines()
    ]
    if additions:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        ignore_path.write_text(existing + separator + "\n".join(additions) + "\n", encoding="utf-8")
    print(json.dumps({"workspace": str(workspace), "initialized": True}, ensure_ascii=False))


def runtime_env(workspace: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        (str(RUNTIME_ROOT / "src"), str(workspace / ".python-deps"), str(RUNTIME_ROOT))
    )
    # Workspace settings take precedence over unrelated parent-process settings.
    path = workspace / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key = line.partition("=")[0].strip()
            if key.startswith(("TRACK_INSIGHT_", "BAILIAN_", "DASHSCOPE_")):
                env.pop(key, None)
    return env


def invoke(workspace: Path, arguments: list[str]) -> int:
    return subprocess.call(
        [sys.executable, "-m", "track_insight", *arguments],
        cwd=workspace,
        env=runtime_env(workspace),
    )


def build_official_dashboard(workspace: Path) -> int:
    return subprocess.call(
        [sys.executable, str(SKILL_ROOT / "scripts" / "official_dashboard.py")],
        cwd=workspace, env=runtime_env(workspace),
    )


def network_check(timeout_seconds: float = 8.0) -> dict[str, object]:
    checks = []
    for host, port, purpose in NETWORK_ENDPOINTS:
        try:
            with socket.create_connection((host, port), timeout=timeout_seconds):
                pass
        except OSError as error:
            checks.append(
                {
                    "purpose": purpose,
                    "host": host,
                    "reachable": False,
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
        else:
            checks.append({"purpose": purpose, "host": host, "reachable": True})
    return {"network_ready": all(item["reachable"] for item in checks), "checks": checks}


def require_network() -> bool:
    result = network_check()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["network_ready"]:
        return True
    print(
        "Outbound network access is unavailable. In a sandboxed Agent environment, "
        "rerun this command with network permission instead of consuming retries.",
        file=sys.stderr,
    )
    return False


def validate_budget(workspace: Path, config_path: str) -> None:
    import yaml

    payload = yaml.safe_load((workspace / config_path).read_text(encoding="utf-8-sig"))
    budget = payload["budgets"]["max_searches_per_work_package"]
    if budget < 4:
        raise ValueError("Coverage planning requires max_searches_per_work_package >= 4")


def doctor(workspace: Path) -> int:
    initialize(workspace)
    dependency_error = None
    probe = subprocess.run(
        [sys.executable, "-c", "import track_insight.cli"],
        cwd=workspace, env=runtime_env(workspace), capture_output=True, text=True,
    )
    if probe.returncode:
        dependency_error = probe.stderr.strip()
    env_text = (workspace / ".env").read_text(encoding="utf-8")
    key_configured = any(
        line.startswith("DASHSCOPE_API_KEY=") and bool(line.partition("=")[2].strip())
        for line in env_text.splitlines()
    )
    database_ok = False
    if dependency_error is None:
        database_ok = invoke(workspace, ["db-init"]) == 0
    payload = {
        "python_supported": (3, 12) <= sys.version_info[:2] < (3, 14),
        "dependencies_ok": dependency_error is None,
        "database_ok": database_ok,
        "api_key_configured": key_configured,
        "config_present": (workspace / "config" / "poc.yaml").is_file(),
        **network_check(),
    }
    if dependency_error:
        payload["dependency_error"] = dependency_error
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    required = ("python_supported", "dependencies_ok", "database_ok", "config_present")
    return 0 if all(payload[key] for key in required) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser("install")
    install.add_argument("--workspace", type=Path, default=Path.cwd())
    direct = subparsers.add_parser("cli")
    direct.add_argument("--workspace", type=Path, default=Path.cwd())
    direct.add_argument("arguments", nargs=argparse.REMAINDER)
    for name in ("init", "doctor", "network-check", "run", "resume", "status", "dashboard"):
        command = subparsers.add_parser(name)
        command.add_argument("--workspace", type=Path, default=Path.cwd())
        if name == "run":
            command.add_argument("--profile", choices=("standard", "demo"), default="standard")
        if name == "resume":
            command.add_argument(
                "--profile", choices=("auto", "standard", "demo"), default="auto"
            )
        if name == "run":
            command.add_argument("--region", action="append", required=True)
            command.add_argument("--industry", action="append", required=True)
            command.add_argument("--start-date", required=True)
            command.add_argument("--end-date", required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if args.command == "install":
        workspace.mkdir(parents=True, exist_ok=True)
        dependencies = tomllib.loads(
            (RUNTIME_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["dependencies"]
        return subprocess.call([
            sys.executable, "-m", "pip", "install", "--target",
            str(workspace / ".python-deps"), *dependencies,
        ])
    sys.path.insert(0, str(workspace / ".python-deps"))
    if args.command == "cli":
        initialize(workspace)
        arguments = args.arguments
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        return invoke(workspace, arguments)
    if args.command == "dashboard":
        return build_official_dashboard(workspace)
    if args.command == "init":
        initialize(workspace)
        return invoke(workspace, ["db-init"])
    if args.command == "doctor":
        return doctor(workspace)
    if args.command == "network-check":
        return 0 if require_network() else 3
    initialize(workspace)
    database_status = invoke(workspace, ["db-init"])
    if database_status != 0:
        return database_status
    if args.command in {"run", "resume"} and not require_network():
        return 3
    if args.command == "run":
        config_path = "config/demo.yaml" if args.profile == "demo" else "config/poc.yaml"
        validate_budget(workspace, config_path)
        (workspace / ".track-insight-profile").write_text(args.profile + "\n", encoding="utf-8")
        cli = [
            "pipeline-run",
            "--config-path",
            config_path,
        ]
        for value in args.region:
            cli += ["--region", value]
        for value in args.industry:
            cli += ["--industry", value]
        cli += ["--start-date", args.start_date, "--end-date", args.end_date]
        status = invoke(workspace, cli)
        return status if status else build_official_dashboard(workspace)
    if args.command == "resume":
        profile = args.profile
        marker = workspace / ".track-insight-profile"
        if profile == "auto":
            profile = marker.read_text(encoding="utf-8").strip() if marker.is_file() else "standard"
        config_path = "config/demo.yaml" if profile == "demo" else "config/poc.yaml"
        validate_budget(workspace, config_path)
        status = invoke(workspace, ["pipeline-run", "--resume", "--config-path", config_path])
        return status if status else build_official_dashboard(workspace)
    return invoke(workspace, ["pipeline-status"])


if __name__ == "__main__":
    raise SystemExit(main())
