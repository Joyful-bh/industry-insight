"""Redefine the frozen national candidate pool and merge bounded packages with resume."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from scripts.event_stage_supervisor import client, save
from scripts.export_market_pilot import export_pilot
from scripts.supervise_track_full import trigger_codex
from track_insight.core.fingerprints import fingerprint
from track_insight.settings import load_poc_config
from track_insight.tracks.bounded_markets import (
    _row,
    comparison_packages,
    identity_text,
    merge_packages_direct,
)
from track_insight.tracks.contracts import CandidateTrackOutput
from track_insight.tracks.evidence_strength import recalculate_evidence_strength
from track_insight.tracks.markets import DirectMarketCatalog, market_output_model


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def run(root):
    config = load_poc_config(Path("config/national.yaml"))
    prompt = config.stage4.prompts.track_market_catalog.read_text(encoding="utf-8")
    state = dict(status="running", stage="definitions", pid=os.getpid())
    lock = threading.RLock()

    def record(**values):
        with lock:
            state.update(values, updated_at=datetime.now(UTC).isoformat())
            save(root / "status.json", state)

    record()
    try:
        definitions = root / "definitions"
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.run_selected_track_redefinition",
                "--all",
                "--output",
                str(definitions),
                "--reuse",
                "data/track_quality/cluster_market_redefinition/run_1",
            ],
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(f"Definition worker failed: {process.returncode}")
        definition_status = load(definitions / "status.json")
        rows = load(definitions / "defined_tracks.json")["tracks"]
        tracks = [CandidateTrackOutput.model_validate(row) for row in rows]
        if definition_status["status"] != "completed" or definition_status["selected"] != len(
            tracks
        ):
            raise ValueError("Incomplete definition pool")
        record(stage="partitioning", definitions=definition_status, input_count=len(tracks))
        vectors = [
            load(
                Path("data/embedding_cache")
                / (
                    fingerprint(config.stage4.grouping_embedding_model, 512, identity_text(t))
                    + ".json"
                )
            )["vector"]
            for t in tracks
        ]
        packages, recall = comparison_packages(tracks, vectors, partitioning="cluster")
        save(root / "preparation.json", dict(packages=packages, **recall))
        scope = dict(
            industry_scopes=config.scope.industry_scopes,
            track_boundary=config.stage4.target_track_boundary,
        )
        snapshot = dict(catalog=prompt, model=config.stage4.model)
        snapshot_path = root / "prompt_snapshot.json"
        if snapshot_path.exists() and load(snapshot_path) != snapshot:
            raise ValueError("Resume merge prompt/model changed")
        save(snapshot_path, snapshot)
        cache = root / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        reused_cache = Path("data/track_quality/cluster_market_pilot/run_3_redefined/cache")
        counts = dict(completed_packages=0, cache_hits=0, model_tasks=0)

        def complete(task, payload, schema):
            key = fingerprint(
                dict(
                    task=task,
                    payload=payload,
                    prompt=prompt,
                    model=config.stage4.model,
                    max_tokens=8192,
                    schema=schema.model_json_schema(),
                )
            )
            path = cache / f"{task}_{key}.json"
            checked = market_output_model(task, payload, schema)
            save(cache / f"input_{key}.json", payload)
            if not path.exists() and (reused_cache / path.name).exists():
                save(path, load(reused_cache / path.name))
            if path.exists():
                output = checked.model_validate(load(path))
                with lock:
                    counts["cache_hits"] += 1
                return output
            active = client(config)
            try:
                result = active.complete_structured(
                    messages=[
                        dict(role="system", content=prompt),
                        dict(role="user", content=json.dumps(payload, ensure_ascii=False)),
                    ],
                    output_model=checked,
                    model=config.stage4.model,
                    max_tokens=8192,
                    enable_thinking=False,
                )
                save(cache / f"response_{key}.json", result.raw_response)
                save(path, result.output.model_dump(mode="json"))
                with lock:
                    counts["model_tasks"] += 1
                return result.output
            except Exception as error:
                diagnostics = getattr(error, "diagnostics", {})
                save(
                    cache / f"failure_{key}_{datetime.now(UTC).timestamp()}.json",
                    dict(error=str(error), **diagnostics),
                )
                raise
            finally:
                active.close()

        def prepare_package(members):
            return complete(
                "direct_catalog",
                dict(scope=scope, tracks=[_row(tracks[i], i) for i in members]),
                DirectMarketCatalog,
            )

        record(stage="merging", package_count=len(packages), **counts)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(prepare_package, members) for members in packages]
            for future in as_completed(futures):
                future.result()
                with lock:
                    counts["completed_packages"] += 1
                    record(**counts)
        # All proposals are cached; assembly uses exactly the production direct-merge logic.
        result, stats = merge_packages_direct(tracks, scope, packages, complete)
        from sqlalchemy.orm import Session

        from track_insight.infrastructure.database import get_engine

        engine = get_engine()
        try:
            with Session(engine) as session:
                audit = recalculate_evidence_strength(session, result)
            save(root / "evidence_scores.json", audit)
        finally:
            engine.dispose()
        save(
            root / "tracks.json",
            dict(
                validation_scope="full_candidate_draft",
                tracks=[t.model_dump(mode="json") for t in result],
            ),
        )
        save(
            root / "pilot.json",
            dict(
                package_ids=list(range(len(packages))),
                selected_ids=list(range(len(tracks))),
                selected_candidates=len(tracks),
                stats=stats,
                selected_tracks=[
                    dict(index=i, **t.model_dump(mode="json")) for i, t in enumerate(tracks)
                ],
            ),
        )
        view = export_pilot(root)
        record(status="completed", stage="completed", stats=stats, view=str(view), **counts)
    except Exception as error:
        record(status="failed", error=f"{type(error).__name__}: {error}")
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--supervise", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if not args.supervise:
        run(args.output)
        return
    codex = shutil.which("codex")
    for attempt in range(3):
        with (args.output / "worker.log").open("a", encoding="utf-8") as stream:
            worker = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "scripts.run_market_full_redefinition",
                    "--output",
                    str(args.output),
                ],
                stdout=stream,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            save(
                args.output / "supervisor_status.json",
                dict(
                    status="watching", pid=os.getpid(), worker_pid=worker.pid, attempt=attempt + 1
                ),
            )
            code = worker.wait()
        status = load(args.output / "status.json")
        if code == 0 and status["status"] == "completed":
            save(args.output / "supervisor_status.json", dict(status="completed", pid=os.getpid()))
            return
        if not codex or attempt == 2:
            break
        try:
            decision = trigger_codex(
                codex,
                args.output,
                dict(
                    returncode=code,
                    status=status,
                    resume_command=(
                        "python -m scripts.run_market_full_redefinition "
                        f"--output {args.output}"
                    ),
                ),
            )
        except Exception as error:
            save(args.output / "supervisor_status.json", dict(status="blocked", error=str(error)))
            return
        if not decision["fixed"]:
            break
    save(args.output / "supervisor_status.json", dict(status="blocked", worker_status=status))


if __name__ == "__main__":
    main()
