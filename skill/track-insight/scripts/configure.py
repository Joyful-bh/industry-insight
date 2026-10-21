#!/usr/bin/env python3
"""Store or remove the Bailian API key without printing it."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path


def update_env(path: Path, key: str, value: str | None) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    prefix = f"{key}="
    kept = [line for line in lines if not line.startswith(prefix)]
    if value is not None:
        kept.append(prefix + value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="command", required=True)
    set_key = subparsers.add_parser("set-key")
    set_key.add_argument("--stdin", action="store_true")
    subparsers.add_parser("clear-key")
    args = parser.parse_args()
    env_path = args.workspace.resolve() / ".env"
    if args.command == "clear-key":
        update_env(env_path, "DASHSCOPE_API_KEY", None)
        print("Bailian API key removed")
        return 0
    value = (
        sys.stdin.readline().strip()
        if args.stdin and not sys.stdin.isatty()
        else getpass.getpass("Bailian API key: ")
    )
    if len(value) < 10 or any(character.isspace() for character in value):
        print("Invalid API key value", file=sys.stderr)
        return 2
    update_env(env_path, "DASHSCOPE_API_KEY", value)
    print("Bailian API key configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
