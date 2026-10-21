"""Install local dependencies and diagnose the user's workspace."""

import argparse
import json
import socket
import subprocess
import sys
from urllib.parse import urlparse

from common import RUNTIME, child_env, initialize, settings


def network(workspace):
    url = settings(workspace).get("DASHSCOPE_BASE_URL", "")
    endpoint = urlparse(url)
    try:
        with socket.create_connection(
            (endpoint.hostname, endpoint.port or (443 if endpoint.scheme == "https" else 80)),
            timeout=8,
        ):
            pass
    except (OSError, TypeError, ValueError):
        return False
    return True


def main():
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init", "install", "doctor", "network-check"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--network", action="store_true")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    initialize(workspace)
    if args.command == "install":
        return subprocess.call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--target",
                str(workspace / ".python-deps"),
                "-r",
                str(RUNTIME / "requirements.txt"),
            ]
        )
    if args.command == "init":
        print(json.dumps({"initialized": True, "workspace": str(workspace)}, ensure_ascii=False))
        return 0
    if args.command == "network-check":
        ready = network(workspace)
        print(json.dumps({"network_ready": ready}))
        return 0 if ready else 3
    probe = subprocess.run(
        [sys.executable, "-c", "import openpyxl,pyarrow,httpx,numpy"],
        cwd=workspace,
        env=child_env(workspace),
        capture_output=True,
        text=True,
    )
    payload = {
        "python_supported": sys.version_info >= (3, 12),
        "dependencies_ok": probe.returncode == 0,
        "api_key_configured": bool(settings(workspace).get("DASHSCOPE_API_KEY")),
        "config_present": (workspace / ".env").is_file(),
    }
    if args.network:
        payload["network_ready"] = network(workspace)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(payload.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
