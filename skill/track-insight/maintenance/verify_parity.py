"""Check source identity and compare the official renderer in separate processes."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
PROJECT = SKILL.parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, required=True)
    args = parser.parse_args()
    subprocess.run([sys.executable, "-B", str(SKILL / "maintenance/sync_runtime.py"), "--check"],
                   check=True)
    code = (
        "import sys,json,hashlib; from pathlib import Path; "
        "from scripts.build_smb_dashboard import render; "
        "p=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')); "
        "print(hashlib.sha256(render(p).encode('utf-8')).hexdigest())"
    )
    hashes = []
    for source in (PROJECT, SKILL / "runtime"):
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join((str(source / "src"), str(source)))
        result = subprocess.run([sys.executable, "-B", "-c", code, str(args.payload.resolve())],
                                cwd=source, env=env, capture_output=True, text=True, check=True)
        hashes.append(result.stdout.strip())
    if hashes[0] != hashes[1]:
        raise RuntimeError("Official renderers produced different HTML")
    print(json.dumps({"html_byte_identical": True, "sha256": hashes[0]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
