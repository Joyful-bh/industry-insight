"""Resolve bundled code and user-owned runtime resources."""

import json
import os
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
RUNTIME = SKILL / "runtime"


def initialize(workspace):
    workspace.mkdir(parents=True, exist_ok=True)
    for name in ("work", "outputs"):
        (workspace / name).mkdir(exist_ok=True)
    env = workspace / ".env"
    if not env.exists():
        env.write_bytes((RUNTIME / "config/defaults.env").read_bytes())
        try:
            env.chmod(0o600)
        except OSError:
            pass
    ignore = workspace / ".gitignore"
    existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    additions = [
        x
        for x in (".env", ".python-deps/", "work/", "outputs/", ".run-state.json")
        if x not in existing.splitlines()
    ]
    if additions:
        ignore.write_text(
            existing.rstrip("\n") + "\n" + "\n".join(additions) + "\n", encoding="utf-8"
        )


def settings(workspace):
    result = {}
    for raw in (workspace / ".env").read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def child_env(workspace):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(workspace / ".python-deps")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for key in list(env):
        if key.startswith("DASHSCOPE_"):
            env.pop(key)
    env.update(settings(workspace))
    return env


def save_state(workspace, value):
    path = workspace / ".run-state.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
