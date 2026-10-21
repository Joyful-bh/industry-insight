"""Copy the authoritative project files and verify the distributable runtime."""
import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
PROJECT = SKILL.parents[1]
TREES = ("src", "config", "scripts", "migrations", "tests", "experiments", "docs/reference")
FILES = ("pyproject.toml", "README.md", "alembic.ini", ".env.example")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--package", action="store_true")
    args = parser.parse_args()
    runtime = SKILL / "runtime"
    sources = [PROJECT / name for name in FILES]
    for name in TREES:
        sources.extend(p for p in (PROJECT / name).rglob("*")
                       if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    mismatches = []
    for source in sources:
        target = runtime / source.relative_to(PROJECT)
        if not args.check:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        if not target.is_file() or digest(source) != digest(target):
            mismatches.append(str(source.relative_to(PROJECT)))
    expected = {p.relative_to(PROJECT).as_posix() for p in sources}
    for p in runtime.rglob("*"):
        if p.is_file() and not {"__pycache__", ".ruff_cache"}.intersection(p.parts) and p.suffix != ".pyc":
            rel = p.relative_to(runtime).as_posix()
            if rel not in expected and rel != "config/demo.yaml":
                mismatches.append("unexpected runtime file: " + rel)
    print(json.dumps({"checked_files": len(sources), "mismatches": mismatches}))
    if mismatches:
        return 1
    if args.package:
        with zipfile.ZipFile(SKILL / "track-insight.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for p in sorted(SKILL.rglob("*")):
                if p.is_file() and p.suffix not in {".zip", ".pyc"} and not {"__pycache__", ".ruff_cache", "maintenance"}.intersection(p.relative_to(SKILL).parts):
                    archive.write(p, p.relative_to(SKILL))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
