"""Exercise a distributed Skill from an unrelated working directory."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--agent-result", type=Path, required=True)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    skill = Path(__file__).resolve().parents[1]
    target = args.target.resolve()
    bundle = target / "分发包 副本"
    workspace = target / "用户数据"
    unrelated = target / "无关当前目录"
    unrelated.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(skill / "track-insight.zip") as archive:
        assert not any("maintenance/" in n for n in archive.namelist())
        archive.extractall(bundle)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(target / "不存在的开发路径")
    env["TRACK_INSIGHT_DATABASE_URL"] = "sqlite:///unrelated-parent.sqlite3"

    def run(script, *arguments):
        subprocess.run([sys.executable, "-B", str(bundle / "scripts" / script), *arguments],
                       cwd=unrelated, env=env, check=True)

    if args.install:
        run("run_pipeline.py", "install", "--workspace", str(workspace))
    # Disable all site-packages to prove that the downloaded local dependencies suffice.
    isolated_env = env.copy()
    isolated_env["PYTHONPATH"] = os.pathsep.join((
        str(bundle / "runtime/src"), str(workspace / ".python-deps"), str(bundle / "scripts")))
    subprocess.run([sys.executable, "-B", "-S", "-c",
                    "import track_insight.cli, bundled_renderer; print('isolated_imports_passed')"],
                   cwd=unrelated, env=isolated_env, check=True)
    run("run_pipeline.py", "init", "--workspace", str(workspace))
    assert (workspace / "data/track_insight.sqlite3").is_file()
    assert not (workspace / "unrelated-parent.sqlite3").exists()
    shutil.copyfile(args.database.resolve(), workspace / "data/track_insight.sqlite3")
    run("run_pipeline.py", "status", "--workspace", str(workspace))
    run("run_pipeline.py", "dashboard", "--workspace", str(workspace))
    run("agent_demo.py", "prepare", "--workspace", str(workspace), "--region", "北京市",
        "--industry", "人形机器人", "--start-date", "2026-01-01", "--end-date", "2026-10-12")
    result = workspace / "agent-demo/result.json"
    shutil.copyfile(args.agent_result.resolve(), result)
    env["PYTHONPATH"] = str(workspace / ".python-deps")
    run("agent_demo.py", "complete", "--workspace", str(workspace), "--result", str(result))
    for name in ("track_dashboard_latest", "agent_demo_dashboard"):
        html = (workspace / "reports" / (name + ".html")).read_text(encoding="utf-8")
        assert "__DATA__" not in html and 'id="province"' in html and 'id="page-size"' in html
        assert 'data-tab="regions"' in html and "Lenovo SMB" in html
    print(json.dumps({"portable": True, "cwd_independent": True,
                      "parent_settings_isolated": True, "official_template": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
