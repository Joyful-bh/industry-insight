"""Replay a fixed small sample through the production SMB association prompt."""

import argparse
import csv
import json
import os
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from scripts.event_stage_supervisor import client, save
from scripts.run_track_full import usage_total
from track_insight.core.fingerprints import fingerprint
from track_insight.settings import load_poc_config
from track_insight.tracks.contracts import SmbAssociationOutput
from track_insight.tracks.service import _load_smb_product_catalog, _smb_analysis_input


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=Path("data/track_quality/smb_association_pilot/run_1")
    )
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--reuse", type=Path)
    args = parser.parse_args()
    config = load_poc_config(Path("config/national.yaml"))
    source = Path("data/track_quality/market_full_redefinition/run_1/tracks.json")
    tracks = json.loads(source.read_text(encoding="utf-8"))["tracks"]
    pool = [(i, t) for i, t in enumerate(tracks) if t["status"] == "candidate"]
    selected = list(enumerate(tracks)) if args.all else random.Random(20261011).sample(pool, 12)
    prompt = config.stage4.prompts.track_analysis.read_text(encoding="utf-8")
    catalog = _load_smb_product_catalog(config.stage4.smb_product_catalog)
    snapshot = {
        "source": str(source),
        "seed": 20261011,
        "model": config.stage4.model,
        "prompt": prompt,
        "catalog": catalog,
        "tracks": [
            {"source_index": i, "name": t["name"], "definition": t["definition"]}
            for i, t in selected
        ],
    }
    snapshot_path = args.output / "inputs.json"
    if snapshot_path.exists() and json.loads(snapshot_path.read_text(encoding="utf-8")) != snapshot:
        raise RuntimeError("Saved inputs differ; use a new output directory")
    save(snapshot_path, snapshot)
    results, errors = [], []
    started_at = datetime.now(UTC).isoformat()

    def progress(status):
        save(
            args.output / "status.json",
            {
                "status": status,
                "pid": os.getpid(),
                "started_at": started_at,
                "updated_at": datetime.now(UTC).isoformat(),
                "model": config.stage4.model,
                "max_concurrency": 4,
                "selected": len(selected),
                "completed": len(results),
                "reused": sum(row["reused"] for row in results),
                "fresh_calls": sum(row["call_count"] for row in results if not row["reused"]),
                "fresh_usage": {
                    k: sum(row["usage"][k] for row in results if not row["reused"])
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                },
                "failed": len(errors),
                "errors": errors,
            },
        )

    def replay(item):
        index, track = item
        payload = _smb_analysis_input(SimpleNamespace(**track), catalog)
        key = fingerprint(payload, prompt, config.stage4.model, "smb-association-pilot-v1")
        path = args.output / "calls" / f"{key}.json"
        for cached in (path, args.reuse / "calls" / path.name if args.reuse else path):
            if cached.exists():
                row = json.loads(cached.read_text(encoding="utf-8"))
                SmbAssociationOutput.model_validate(row)
                if row["name"] != track["name"] or row["definition"] != track["definition"]:
                    raise RuntimeError("Cached track does not match input")
                row.update(source_index=index, reused=True)
                save(path, row)
                return row
        active = client(config)
        try:
            result = active.complete_structured(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                output_model=SmbAssociationOutput,
                model=config.stage4.model,
                max_tokens=2048,
                enable_thinking=False,
            )
            row = {
                "source_index": index,
                "name": track["name"],
                "definition": track["definition"],
                **result.output.model_dump(mode="json"),
                "usage": usage_total(result.raw_response),
                "call_count": result.call_count,
                "reused": False,
            }
            save(path.with_suffix(".response.json"), result.raw_response)
            save(path, row)
            return row
        finally:
            active.close()

    progress("running")
    with ThreadPoolExecutor(max_workers=4) as executor:
        tasks = {executor.submit(replay, item): item for item in selected}
        for future in as_completed(tasks):
            try:
                row = future.result()
                results.append(row)
                print(f"completed: {row['name']}", flush=True)
            except Exception as error:
                errors.append({"name": tasks[future][1]["name"], "error": str(error)})
            progress("running")
    results.sort(key=lambda row: row["source_index"])
    save(
        args.output / "results.json",
        {
            "results": results,
            "usage": {
                k: sum(row["usage"][k] for row in results)
                for k in ("prompt_tokens", "completion_tokens", "total_tokens")
            },
            "call_count": sum(row["call_count"] for row in results),
        },
    )
    if args.all and not errors:
        enriched = json.loads(source.read_text(encoding="utf-8"))
        for row in results:
            enriched["tracks"][row["source_index"]]["smb_value_analysis"] = row[
                "smb_value_analysis"
            ]
        enriched["smb_analysis"] = {
            "scope": "potential_associations",
            "model": config.stage4.model,
            "prompt_fingerprint": fingerprint(prompt),
            "catalog_fingerprint": fingerprint(catalog),
            "notice": "潜在关联，不代表已确认需求或采购意向。",
        }
        save(args.output / "tracks.json", enriched)
    with (args.output / "SMB关联结果.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["赛道名称", "赛道定义", "业务场景/工作负载", "关联品类/服务", "关联理由"])
        for row in results:
            for association in row["smb_value_analysis"] or [{}]:
                writer.writerow(
                    [
                        row["name"],
                        row["definition"],
                        association.get("workload", ""),
                        association.get("product_category", ""),
                        association.get("reason", ""),
                    ]
                )
    progress("failed" if errors else "completed")
    if errors:
        raise RuntimeError("Some pilot calls failed; successful results are cached")


if __name__ == "__main__":
    main()
