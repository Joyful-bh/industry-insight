"""Prepare full recall, then validate a reproducible small selection without publishing."""

import argparse
import json
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from scripts.event_stage_supervisor import client, save
from track_insight.core.fingerprints import fingerprint
from track_insight.settings import load_poc_config
from track_insight.tracks.bounded_markets import (
    comparison_packages,
    identity_text,
    merge_packages,
    merge_packages_direct,
)
from track_insight.tracks.contracts import CandidateTrackOutput
from track_insight.tracks.markets import market_output_model


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/track_quality/track_full/run_5/defined_tracks.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--pilot-count", type=int, default=8)
    parser.add_argument("--package-ids", type=int, nargs="+")
    parser.add_argument("--direct", action="store_true")
    parser.add_argument("--partitioning", choices=("star", "cluster"), default="star")
    parser.add_argument(
        "--direct-prompt", type=Path, default=Path("config/prompts/track_market_coarse_v1.md")
    )
    args = parser.parse_args()
    if args.pilot_count < 1 or args.pilot_count > 12:
        parser.error("pilot-count must be between 1 and 12")
    config = load_poc_config(Path("config/national.yaml"))
    tracks = [
        CandidateTrackOutput.model_validate(row)
        for row in json.loads(args.input.read_text(encoding="utf-8"))["tracks"]
    ]
    cache = args.output / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    shared_embedding_cache = Path("data/embedding_cache")
    previous = args.output / "status.json"
    prior_usage = (
        json.loads(previous.read_text(encoding="utf-8")).get("usage", [])
        if previous.exists()
        else []
    )
    state = dict(
        status="running",
        stage="embeddings",
        input_count=len(tracks),
        started_at=datetime.now(UTC).isoformat(),
        usage=prior_usage,
        cache_hits=0,
    )

    def record(**values):
        state.update(values, updated_at=datetime.now(UTC).isoformat())
        save(args.output / "status.json", state)

    active = client(config)
    record()
    try:
        texts = [identity_text(track) for track in tracks]

        def embed(batch):
            key = fingerprint(dict(model=config.stage4.grouping_embedding_model, texts=batch))
            path = cache / f"embedding_{key}.json"
            if path.exists():
                result = json.loads(path.read_text(encoding="utf-8"))
                cached = True
            else:
                shared_paths = [
                    shared_embedding_cache
                    / (fingerprint(config.stage4.grouping_embedding_model, 512, text) + ".json")
                    for text in batch
                ]
                if all(path.exists() for path in shared_paths):
                    return {
                        "vectors": [
                            json.loads(path.read_text(encoding="utf-8"))["vector"]
                            for path in shared_paths
                        ],
                        "usage": {},
                    }, True
                result = active.embed_texts(batch, model=config.stage4.grouping_embedding_model)
                save(path, result)
                cached = False
            for text, vector in zip(batch, result["vectors"], strict=True):
                shared = Path("data/embedding_cache") / (
                    fingerprint(config.stage4.grouping_embedding_model, 512, text) + ".json"
                )
                if not shared.exists():
                    save(shared, {"vector": vector})
            return result, cached

        vectors = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            for result, cached in pool.map(
                embed, [texts[i : i + 10] for i in range(0, len(texts), 10)]
            ):
                vectors.extend(result["vectors"])
                state["cache_hits"] += cached
                if not cached:
                    state["usage"].append(dict(task="embedding", usage=result["usage"]))
                record(embedded_candidates=len(vectors))
        packages, recall = comparison_packages(tracks, vectors, partitioning=args.partitioning)
        save(args.output / "preparation.json", dict(packages=packages, **recall))
        record(stage="prepared", recall={k: v for k, v in recall.items() if k != "edges"})
        if args.prepare_only:
            record(status="completed")
            return
        # Role diversity, package-size diversity and seeded random samples; no gold labels.
        chosen = []
        for role in sorted({str(t.supplier_role) for t in tracks}):
            eligible = [
                i for i, p in enumerate(packages) if str(tracks[p[0]].supplier_role) == role
            ]
            if eligible:
                chosen.append(max(eligible, key=lambda i: len(packages[i])))
        order = sorted(range(len(packages)), key=lambda i: (len(packages[i]), i))
        for q in (0.0, 0.5, 1.0):
            if order:
                chosen.append(order[int(q * (len(order) - 1))])
        random_order = list(range(len(packages)))
        random.Random(20261011).shuffle(random_order)
        chosen = list(dict.fromkeys(chosen + random_order))[: args.pilot_count]
        if args.package_ids is not None:
            if (
                not 1 <= len(args.package_ids) <= 12
                or len(args.package_ids) != len(set(args.package_ids))
                or any(i < 0 or i >= len(packages) for i in args.package_ids)
            ):
                raise ValueError("package-ids must be 1 to 12 distinct existing package indices")
            chosen = args.package_ids
        selected = [packages[i] for i in chosen]
        prompts = {
            task: getattr(config.stage4.prompts, "track_market_" + task).read_text(encoding="utf-8")
            for task in ("catalog", "audit")
        }
        if args.direct:
            prompts["direct_catalog"] = args.direct_prompt.read_text(encoding="utf-8")
        save(args.output / "prompt_snapshot.json", prompts)

        def complete(task, payload, schema):
            key = fingerprint(
                dict(
                    task=task,
                    payload=payload,
                    prompt=prompts[task],
                    model=config.stage4.model,
                    max_tokens=8192,
                    schema=schema.model_json_schema(),
                )
            )
            path = cache / f"{task}_{key}.json"
            checked = market_output_model(task, payload, schema)
            save(cache / f"input_{key}.json", payload)
            if path.exists():
                state["cache_hits"] += 1
                return checked.model_validate(json.loads(path.read_text(encoding="utf-8")))
            record(stage=task, active_input_chars=len(json.dumps(payload, ensure_ascii=False)))
            try:
                result = active.complete_structured(
                    messages=[
                        dict(role="system", content=prompts[task]),
                        dict(role="user", content=json.dumps(payload, ensure_ascii=False)),
                    ],
                    output_model=checked,
                    model=config.stage4.model,
                    max_tokens=8192,
                    enable_thinking=False,
                )
            except Exception as exc:
                diagnostics = getattr(exc, "diagnostics", {})
                if diagnostics:
                    save(cache / f"failure_{key}_{datetime.now(UTC).timestamp()}.json", diagnostics)
                raise
            save(cache / f"response_{key}.json", result.raw_response)
            save(path, result.output.model_dump(mode="json"))
            state["usage"].append(dict(task=task, usage=result.usage, call_count=result.call_count))
            record(completed_model_tasks=len(state["usage"]))
            return result.output

        scope = dict(
            industry_scopes=config.scope.industry_scopes,
            track_boundary=config.stage4.target_track_boundary,
        )
        merge = merge_packages_direct if args.direct else merge_packages
        result, stats = merge(tracks, scope, selected, complete)
        save(
            args.output / "tracks.json",
            dict(
                validation_scope="partial_pilot",
                untouched_candidates_retained=True,
                tracks=[t.model_dump(mode="json") for t in result],
            ),
        )
        selected_ids = sorted({i for p in selected for i in p})
        # Raw responses include initial invalid outputs, not only successful repairs.
        responses = []
        for path in cache.glob("response_*.json"):
            raw = json.loads(path.read_text(encoding="utf-8"))
            responses.extend(
                [raw["initial_response"], raw["repair_response"]]
                if "initial_response" in raw else [raw]
            )
        for path in cache.glob("failure_*.json"):
            raw = json.loads(path.read_text(encoding="utf-8"))
            responses.extend(
                raw[key] for key in ("initial_response", "recovery_response", "response")
                if isinstance(raw.get(key), dict)
            )
        recorded_usage = dict(
            calls=len(responses),
            **{
                key: sum(response.get("usage", {}).get(key, 0) for response in responses)
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            },
        )
        save(
            args.output / "pilot.json",
            dict(
                package_ids=chosen,
                selected_ids=selected_ids,
                selected_candidates=len(selected_ids),
                recorded_response_usage=recorded_usage,
                stats=stats,
                selected_tracks=[
                    dict(index=i, **tracks[i].model_dump(mode="json")) for i in selected_ids
                ],
            ),
        )
        record(
            status="completed",
            stage="completed",
            stats=stats,
            selected_candidates=len(selected_ids),
            recorded_response_usage=recorded_usage,
        )
    except Exception as exc:
        record(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        active.close()


if __name__ == "__main__":
    main()
