import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".agents" / "skills" / "track-insight" / "scripts"


def _run(script: str, *args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *(str(arg) for arg in args)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_skill_workspace_process_and_evidence_validation() -> None:
    workspace = ROOT / "results" / f".skill-flow-test-{os.getpid()}"
    try:
        initialized = _run(
            "init_workspace.py",
            "--topic",
            "制造业",
            "--region",
            "北京市",
            "--start-date",
            "2024-09-20",
            "--end-date",
            "2026-09-20",
            "--output",
            workspace,
        )
        assert initialized.returncode == 0, initialized.stderr

        source_id = "src_demo"
        page_id = "page_demo"
        event_id = "evt_demo"
        topic_id = "topic_demo"
        evidence_id = "evd_demo"
        page_text = "2025年5月，北京某机器人企业完成100台工业机器人产线设备交付。"
        (workspace / "pages" / f"{source_id}.md").write_text(page_text + "\n", encoding="utf-8")
        source = {
            "source_id": source_id,
            "url": "https://example.com/news/1",
            "canonical_url": "https://example.com/news/1",
            "local_path": None,
            "title": "机器人产线交付",
            "publisher": "示例企业",
            "source_domain": "example.com",
            "domain": "example.com",
            "source_class": "company",
            "url_type": "content_page",
            "possible_published_at": "2025-05-20",
            "decision": "keep",
            "decision_reason": "包含企业项目交付事实",
            "search_task_ids": ["search_demo_1", "search_demo_2"],
            "discoveries": [],
            "acquisition_status": "usable",
            "page_path": f"pages/{source_id}.md",
            "content_hash": "demo",
            "error": None,
        }
        page = {
            "page_id": page_id,
            "source_id": source_id,
            "requested_url": source["url"],
            "final_url": source["url"],
            "content_type": "html",
            "acquisition_method": "http_html",
            "http_status": 200,
            "title": source["title"],
            "published_at": "2025-05-20",
            "content_path": source["page_path"],
            "content_chars": len(page_text),
            "content_hash": "demo",
            "quality_status": "usable",
            "quality_reasons": [],
            "relevance": "relevant",
            "document_type": "enterprise_news",
            "review_reason": "与研究范围直接相关",
            "review_regions": ["北京市"],
            "review_industries": ["制造业"],
            "content_sufficient": True,
            "fetched_at": "2026-09-20T10:00:00+08:00",
        }
        evidence = {
            "evidence_id": evidence_id,
            "quote": page_text,
            "source_id": source_id,
            "page_id": page_id,
            "content_path": source["page_path"],
            "start_offset": 0,
            "end_offset": len(page_text),
            "evidence_type": "local_text",
        }
        event = {
            "event_id": event_id,
            "source_id": source_id,
            "page_id": page_id,
            "event_type": "project_progress",
            "event_status": "completed",
            "title": "机器人产线设备交付",
            "summary": "2025年该企业完成100台工业机器人产线设备交付。",
            "signal_date": "2025-05-01",
            "date_precision": "month",
            "regions": ["北京市"],
            "industries": ["制造业"],
            "industry_objects": ["工业机器人产线设备"],
            "topic_hint": "工业机器人系统集成",
            "entities": [{"name": "北京某机器人企业", "type": "company"}],
            "chain_roles": ["系统集成"],
            "smb_relevance": "high",
            "smb_reason": "系统集成企业具有可复制的工程设计与交付工作负载。",
            "confidence": 0.9,
            "evidence": [evidence],
            "evidence_coverage": {"subject": [0], "action": [0], "date": [0], "numbers": [0]},
            "event_fingerprint": "event-demo",
            "duplicate_group_id": None,
        }
        topic = {
            "topic_id": topic_id,
            "canonical_key": "industrial-robot-integration",
            "label": "工业机器人系统集成",
            "definition": "为制造企业设计、部署和交付工业机器人产线的经营活动。",
            "aliases": [],
            "keywords": ["工业机器人", "系统集成"],
            "regions": ["北京市"],
            "industries": ["制造业"],
            "summary": "机器人产线交付活动形成产业信号。",
            "event_ids": [event_id],
            "first_seen_at": "2025-05-01",
            "latest_seen_at": "2025-05-01",
            "confidence": 0.86,
            "status": "candidate",
        }
        track = {
            "track_id": "track_demo",
            "canonical_key": "industrial-robot-system-integrators",
            "name": "工业机器人系统集成服务",
            "aliases": [],
            "definition": "面向制造企业交付机器人产线方案的系统集成企业集合。",
            "topic_memberships": [
                {
                    "topic_id": topic_id,
                    "role": "core",
                    "relevance_score": 0.95,
                    "reason": "直接描述核心经营活动",
                }
            ],
            "enterprise_archetype": (
                "具备机器人选型、产线设计、现场部署和运维能力的中小型系统集成商。"
            ),
            "core_products_services": ["机器人产线集成"],
            "core_company_types": ["工业机器人系统集成商"],
            "supporting_company_types": ["机器人本体厂商"],
            "shared_demand_drivers": ["制造业自动化改造"],
            "included_activities": ["产线设计", "现场部署"],
            "excluded_activities": ["仅销售通用机器人本体"],
            "chain_roles": ["系统集成"],
            "observable_company_features": ["官网展示机器人产线交付案例"],
            "possible_it_needs": ["移动工作站", "项目数据管理"],
            "supporting_event_ids": [event_id],
            "regions": ["北京市"],
            "granularity_assessment": {"valid": True, "reason": "对应明确企业集合"},
            "analysis": {
                "summary": "系统集成企业形成可观察的交付型赛道。",
                "why_now": "近期出现机器人产线交付信号。",
                "signal_statistics": {"independent_event_count": 1, "source_count": 1},
                "activity_assessment": {
                    "label": "insufficient_history",
                    "historical_comparability": False,
                    "basis": "当前仅有一条流程测试信号。",
                },
                "industry_chain_analysis": "企业连接机器人本体、控制系统与终端制造客户。",
                "smb_value_analysis": [
                    {
                        "business_activity": "设计并交付机器人产线",
                        "workload": "工程设计、仿真、项目文档和现场协作",
                        "it_need": "移动工作站、集中存储和项目协作",
                        "coverage_path": "通过集成案例和项目招投标信息识别企业",
                    }
                ],
                "evidence_event_ids": [event_id],
                "uncertainties": ["仅用于流程验证，不代表真实赛道结论"],
            },
            "confidence": 0.8,
            "status": "candidate",
        }
        _write_jsonl(workspace / "sources.jsonl", [source])
        _write_json(
            workspace / "work-packages.json",
            {
                "schema_version": "work-package-v1",
                "minimum_unique_content_urls": 1,
                "minimum_source_classes": 1,
                "maximum_domain_ratio": 1.0,
                "work_packages": [
                    {
                        "work_package_id": "wp_demo",
                        "name": "机器人产业化",
                        "purpose": "验证企业项目和产业信号",
                        "target_regions": ["北京市"],
                        "target_industries": ["制造业"],
                        "target_signal_types": ["project_progress"],
                        "target_source_classes": ["company"],
                        "completion_criteria": "执行全部查询并取得企业项目页面",
                        "minimum_retained_urls": 1,
                        "status": "completed",
                    }
                ],
            },
        )
        _write_jsonl(
            workspace / "search-tasks.jsonl",
            [
                {
                    "search_task_id": task_id,
                    "work_package_id": "wp_demo",
                    "query": query,
                    "purpose": "验证企业项目信号",
                    "target_regions": ["北京市"],
                    "target_industries": ["制造业"],
                    "target_signal_types": ["project_progress"],
                    "target_source_classes": ["company"],
                    "status": "completed",
                    "searched_at": "2026-09-20T10:00:00+08:00",
                    "result_count": 1,
                    "discovered_source_ids": [source_id],
                    "error": None,
                }
                for task_id, query in (
                    ("search_demo_1", "北京 机器人 产线 交付"),
                    ("search_demo_2", "北京 机器人 企业 项目"),
                )
            ],
        )
        _write_jsonl(workspace / "pages" / "index.jsonl", [page])
        _write_jsonl(workspace / "events.jsonl", [event])
        _write_jsonl(
            workspace / "evidence" / "evidence-index.jsonl",
            [{**evidence, "event_id": event_id, "source_url": source["url"], "quote_hash": "demo"}],
        )
        _write_json(
            workspace / "topics.json",
            {"schema_version": "topic-v1", "topics": [topic], "unassigned_event_ids": []},
        )
        _write_json(workspace / "tracks.json", {"schema_version": "track-v1", "tracks": [track]})

        research_gate = _run("check_research_gate.py", workspace, "--write")
        assert research_gate.returncode == 0, research_gate.stdout

        validated = _run("validate_workspace.py", workspace, "--write")
        assert validated.returncode == 0, validated.stdout
        assert json.loads(validated.stdout)["valid"] is True

        dashboard = _run("build_dashboard.py", workspace)
        assert dashboard.returncode == 0, dashboard.stderr
        assert "工业机器人系统集成服务" in (workspace / "dashboard.html").read_text(
            encoding="utf-8"
        )

        (workspace / "report.md").write_text("# 模拟流程验证报告\n", encoding="utf-8")
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        manifest["status"] = "completed"
        manifest["completed_at"] = "2026-09-20T18:00:00+08:00"
        manifest["counts"] = {
            "sources": 1,
            "usable_pages": 1,
            "events": 1,
            "topics": 1,
            "candidate_tracks": 1,
            "watchlist_tracks": 0,
        }
        _write_json(workspace / "manifest.json", manifest)
        checkpoint = json.loads((workspace / "checkpoint.json").read_text(encoding="utf-8"))
        checkpoint["current_stage"] = "completed"
        checkpoint["completed_stages"] = [
            "planning",
            "discovery",
            "acquisition",
            "event_extraction",
            "topic_synthesis",
            "track_synthesis",
            "track_analysis",
            "validation",
            "reporting",
        ]
        _write_json(workspace / "checkpoint.json", checkpoint)
        completed = _run("validate_workspace.py", workspace)
        assert completed.returncode == 0, completed.stdout

        incremental = workspace / "incremental"
        incremental_result = _run(
            "init_workspace.py",
            "--topic",
            "制造业",
            "--region",
            "北京市",
            "--start-date",
            "2024-09-20",
            "--end-date",
            "2026-09-20",
            "--existing-result",
            workspace,
            "--output",
            incremental,
        )
        assert incremental_result.returncode == 0, incremental_result.stderr
        child_manifest = json.loads(
            (incremental / "manifest.json").read_text(encoding="utf-8")
        )
        assert child_manifest["parent_research_id"] == manifest["research_id"]
        assert (incremental / "events.jsonl").read_text(encoding="utf-8")

        event["evidence"][0]["quote"] = "正文中不存在的证据"
        _write_jsonl(workspace / "events.jsonl", [event])
        rejected = _run("validate_workspace.py", workspace)
        assert rejected.returncode == 1
        assert "unresolvable_quote" in rejected.stdout
    finally:
        if workspace.is_dir():
            shutil.rmtree(workspace)


def test_skill_url_normalization() -> None:
    result = _run(
        "normalize_urls.py",
        "HTTPS://Example.COM:443/path/?utm_source=x&b=2&a=1#fragment",
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "https://example.com/path?a=1&b=2"


def test_skill_fetches_and_extracts_a_local_html_source() -> None:
    workspace = ROOT / "results" / f".skill-fetch-test-{os.getpid()}"
    try:
        initialized = _run(
            "init_workspace.py",
            "--topic",
            "制造业",
            "--region",
            "北京市",
            "--start-date",
            "2024-09-20",
            "--end-date",
            "2026-09-20",
            "--output",
            workspace,
        )
        assert initialized.returncode == 0, initialized.stderr
        local_html = workspace / "input.html"
        local_html.write_text(
            "<html><head><title>本地材料</title></head>"
            "<body><nav>导航</nav><main>北京制造企业完成数字化产线建设。</main></body></html>",
            encoding="utf-8",
        )
        source = {
            "source_id": "src_local",
            "url": None,
            "canonical_url": None,
            "local_path": str(local_html),
            "title": "本地材料",
            "publisher": None,
            "source_domain": None,
            "source_class": "other",
            "url_type": "content_page",
            "possible_published_at": None,
            "decision": "keep",
            "decision_reason": "用户指定文件",
            "discoveries": [],
            "acquisition_status": "pending",
            "page_path": None,
            "content_hash": None,
            "error": None,
        }
        _write_jsonl(workspace / "sources.jsonl", [source])
        fetched = _run("fetch_pages.py", workspace, "--min-chars", 10)
        assert fetched.returncode == 0, fetched.stderr
        result = json.loads(fetched.stdout)
        assert result["usable"] == 1
        page_text = (workspace / "pages" / "src_local.md").read_text(encoding="utf-8")
        assert "北京制造企业完成数字化产线建设" in page_text
        assert "导航" not in page_text
    finally:
        if workspace.is_dir():
            shutil.rmtree(workspace)
