"""Build a distribution from a caller-selected project, without bundling user data."""

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve()
    files = [source / "scripts/run_company_track_pipeline.py", source / "requirements.txt"]
    for folder in ("scripts/pipeline", "scripts/tracks"):
        files.extend(
            p
            for p in (source / folder).rglob("*")
            if p.is_file() and p.suffix in {".py", ".md", ".html"}
        )
    mismatches = []
    for path in files:
        target = SKILL / "runtime" / path.relative_to(source)
        if not args.check:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        if (
            not target.is_file()
            or hashlib.sha256(path.read_bytes()).digest()
            != hashlib.sha256(target.read_bytes()).digest()
        ):
            mismatches.append(path.relative_to(source).as_posix())
    if mismatches:
        raise RuntimeError("Business implementation mismatch: " + ", ".join(mismatches))
    archive_path = SKILL / "company-track-labeling.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for top in ("SKILL.md", "agents", "references", "scripts", "runtime"):
            root = SKILL / top
            paths = [root] if root.is_file() else sorted(root.rglob("*"))
            for path in paths:
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    archive.write(path, path.relative_to(SKILL))
    print(
        json.dumps(
            {"business_files_checked": len(files), "archive": str(archive_path)}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
