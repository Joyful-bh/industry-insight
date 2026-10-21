#!/usr/bin/env python3
"""Offline end-to-end smoke test for the Agent-native handoff."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path


def main() -> int:
    script = Path(__file__).with_name("agent_demo.py")
    workspace = Path.cwd() / f".skill-smoke-{uuid.uuid4().hex}"
    workspace.mkdir()
    try:
        subprocess.run(
            [sys.executable, str(script), "prepare", "--workspace", str(workspace),
             "--region", "北京市", "--industry", "工业软件", "--start-date", "2026-01-01",
             "--end-date", "2026-09-30"], check=True, capture_output=True, text=True
        )
        result = {
            "summary": "离线冒烟测试结果。",
            "coverage_note": "固定样例，仅验证数据交接、存储和看板链路。",
            "tracks": [{
                "name": "工业软件服务演示赛道", "definition": "用于验证流程的样例定义。",
                "why_now": "固定测试材料显示存在近期活动。",
                "smb_value": "验证设备与服务映射展示。",
                "confidence": 0.8,
                "smb_value_analysis": [{
                    "workload": "工程仿真", "product_category": "工作站",
                    "reason": "测试结构化 SMB 关联的数据交接。",
                }],
                "evidence": [{"title": "测试来源", "url": "https://example.com/source",
                              "quote": "仅用于离线链路测试的固定证据。", "date": "2026-09-01"}],
            }],
        }
        result_path = workspace / "agent-demo" / "result.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        subprocess.run(
            [sys.executable, str(script), "complete", "--workspace", str(workspace),
             "--result", str(result_path)], check=True, capture_output=True, text=True
        )
        dashboard = workspace / "reports" / "agent_demo_dashboard.html"
        database = workspace / "state.sqlite3"
        if not dashboard.is_file() or dashboard.stat().st_size < 500:
            raise RuntimeError("dashboard was not generated")
        with sqlite3.connect(database) as connection:
            if connection.execute("SELECT count(*) FROM demo_track").fetchone()[0] != 1:
                raise RuntimeError("track was not persisted")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    print("offline end-to-end smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
