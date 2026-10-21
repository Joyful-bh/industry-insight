#!/usr/bin/env python3
"""Run the complete company-to-track classification pipeline from two inputs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PIPELINE_SCRIPTS = SCRIPTS / "pipeline"


def run_step(index: int, total: int, label: str, command: list[str]) -> None:
    print(f"\n[{index}/{total}] {label}", flush=True)
    print("  " + " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--companies", type=Path, required=True, help="整理好的企业名单 XLSX")
    parser.add_argument(
        "--tracks", type=Path, required=True,
        help="整理好的赛道 JSON 或 JSONL（classification-ready schema）",
    )
    parser.add_argument("--output", type=Path, help="追加赛道判断列后的企业 XLSX")
    parser.add_argument("--sheet", help="企业工作表名称；默认使用第一个工作表")
    parser.add_argument(
        "--work-dir", type=Path,
        default=Path("data/processed/company_track_pipeline"),
        help="可恢复的中间结果与缓存目录",
    )
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--candidate-budget", type=int, default=2000)
    parser.add_argument("--core-budget", type=int, default=1600)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument(
        "--advanced-search", action="store_true",
        help="初级搜索完成后，对仍因信息不足而未决的企业启用昂贵的 Responses API 高级搜索",
    )
    parser.add_argument(
        "--force-model-calls", action="store_true",
        help="忽略本地核验和联网检索缓存并重新调用模型",
    )
    args = parser.parse_args()
    if args.companies.suffix.lower() != ".xlsx":
        parser.error("--companies must be an XLSX file")
    if args.tracks.suffix.lower() not in {".json", ".jsonl"}:
        parser.error("--tracks must be a JSON or JSONL file")
    if not 1 <= args.concurrency <= 8:
        parser.error("--concurrency must be between 1 and 8")
    if args.candidate_budget < 1:
        parser.error("--candidate-budget must be positive")
    if not 1 <= args.core_budget <= args.candidate_budget:
        parser.error("--core-budget must be between 1 and --candidate-budget")
    return args


def main() -> int:
    args = parse_args()
    companies = args.companies.resolve()
    tracks = args.tracks.resolve()
    env_file = args.env.resolve()
    if not companies.exists():
        raise FileNotFoundError(companies)
    if not tracks.exists():
        raise FileNotFoundError(tracks)
    if not env_file.exists():
        raise FileNotFoundError(env_file)

    output = (
        args.output.resolve()
        if args.output
        else companies.with_name(f"{companies.stem}_赛道判断.xlsx")
    )
    if output == companies:
        raise ValueError("--output must differ from --companies")
    work = args.work_dir.resolve()
    companies_dir = work / "companies"
    tracks_dir = work / "tracks"
    embeddings_dir = work / "embeddings"
    candidates_dir = work / "candidates"
    local_dir = work / "local_verification"
    basic_dir = work / "web_basic"
    advanced_dir = work / "web_advanced"
    cache_dir = work / "cache"
    for path in (work, cache_dir):
        path.mkdir(parents=True, exist_ok=True)

    python = sys.executable
    common_concurrency = str(args.concurrency)
    force = ["--force"] if args.force_model_calls else []
    steps: list[tuple[str, list[str]]] = []

    prepare_companies = [
        python, str(PIPELINE_SCRIPTS / "prepare_companies_for_classification.py"),
        "--input", str(companies), "--output-dir", str(companies_dir),
    ]
    if args.sheet:
        prepare_companies.extend(["--sheet", args.sheet])
    steps.append(("准备企业分类数据", prepare_companies))
    steps.append(("准备赛道召回数据", [
        python, str(PIPELINE_SCRIPTS / "prepare_tracks_for_retrieval.py"),
        "--input", str(tracks), "--output-dir", str(tracks_dir),
    ]))
    steps.append(("生成或复用语义向量", [
        python, str(PIPELINE_SCRIPTS / "generate_semantic_embeddings.py"),
        "--env", str(env_file),
        "--companies", str(companies_dir / "companies_classification_ready.parquet"),
        "--tracks", str(tracks_dir / "tracks_retrieval_ready.parquet"),
        "--output-dir", str(embeddings_dir),
        "--cache", str(cache_dir / "embedding_cache.sqlite"),
        "--concurrency", common_concurrency,
    ]))
    steps.append(("执行BM25与语义向量混合召回", [
        python, str(PIPELINE_SCRIPTS / "retrieve_hybrid_candidates_operational.py"),
        "--companies", str(companies_dir / "companies_classification_ready.parquet"),
        "--tracks", str(tracks_dir / "tracks_retrieval_ready.parquet"),
        "--embedding-dir", str(embeddings_dir),
        "--output-dir", str(candidates_dir),
        "--candidate-budget", str(args.candidate_budget),
        "--core-budget", str(args.core_budget),
    ]))
    steps.append(("批量核验本地证据", [
        python, str(PIPELINE_SCRIPTS / "verify_candidate_evidence_api.py"),
        "--candidates", str(candidates_dir / "candidates_hybrid.parquet"),
        "--companies", str(companies_dir / "companies_classification_ready.parquet"),
        "--tracks", str(tracks_dir / "tracks_retrieval_ready.parquet"),
        "--env", str(env_file),
        "--raw-dir", str(cache_dir / "local_verification_raw"),
        "--output-dir", str(local_dir),
        "--candidate-limit", str(args.candidate_budget),
        "--concurrency", common_concurrency,
        *force,
    ]))
    steps.append(("初级联网补充与复判", [
        python, str(PIPELINE_SCRIPTS / "web_recheck_unresolved_companies.py"),
        "--stage", "all", "--search-tier", "basic",
        "--candidates", str(local_dir / "candidate_decisions.csv"),
        "--companies", str(companies_dir / "companies_classification_ready.parquet"),
        "--tracks", str(tracks_dir / "tracks_retrieval_ready.parquet"),
        "--output-dir", str(basic_dir),
        "--enrichment-raw-dir", str(cache_dir / "web_basic_enrichment_raw"),
        "--adjudication-raw-dir", str(cache_dir / "web_basic_adjudication_raw"),
        "--env", str(env_file), "--concurrency", common_concurrency,
        *force,
    ]))
    decision_file = basic_dir / "candidate_decisions_final.csv"
    advanced_command: list[str] | None = None
    if args.advanced_search:
        advanced_command = [
            python, str(PIPELINE_SCRIPTS / "web_recheck_unresolved_companies.py"),
            "--stage", "all", "--search-tier", "advanced",
            "--escalate-from", str(basic_dir),
            "--companies", str(companies_dir / "companies_classification_ready.parquet"),
            "--tracks", str(tracks_dir / "tracks_retrieval_ready.parquet"),
            "--output-dir", str(advanced_dir),
            "--enrichment-raw-dir", str(cache_dir / "web_advanced_enrichment_raw"),
            "--adjudication-raw-dir", str(cache_dir / "web_advanced_adjudication_raw"),
            "--env", str(env_file), "--concurrency", common_concurrency,
            *force,
        ]

    started = time.time()
    total_steps = len(steps) + 1 + int(args.advanced_search)
    for index, (label, command) in enumerate(steps, start=1):
        run_step(index, total_steps, label, command)

    advanced_ran = False
    if advanced_command:
        basic_report = json.loads((basic_dir / "final_report.json").read_text(encoding="utf-8"))
        target_count = int(basic_report.get("web_information_insufficient_count", 0))
        if target_count:
            run_step(len(steps) + 1, total_steps, "高级搜索升级与复判", advanced_command)
            decision_file = advanced_dir / "candidate_decisions_final.csv"
            advanced_ran = True
        else:
            print(
                f"\n[{len(steps) + 1}/{total_steps}] 无信息不足候选，跳过高级搜索",
                flush=True,
            )

    export_command = [
        python, str(PIPELINE_SCRIPTS / "export_company_track_results_excel.py"),
        "--input", str(companies),
        "--decisions", str(decision_file),
        "--output", str(output),
    ]
    if args.sheet:
        export_command.extend(["--sheet", args.sheet])
    run_step(total_steps, total_steps, "将赛道判断追加到企业工作簿", export_command)

    manifest = {
        "schema_version": "company-track-pipeline-1.0",
        "companies": str(companies),
        "tracks": str(tracks),
        "output": str(output),
        "work_dir": str(work),
        "search_mode": "basic_then_advanced" if advanced_ran else "basic",
        "advanced_search_requested": args.advanced_search,
        "concurrency": args.concurrency,
        "candidate_budget": args.candidate_budget,
        "core_budget": args.core_budget,
        "final_decisions": str(decision_file),
        "elapsed_seconds": round(time.time() - started, 2),
    }
    (work / "pipeline_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n完成：{output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
