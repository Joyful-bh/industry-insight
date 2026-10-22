from __future__ import annotations

# ruff: noqa: E402 -- Prefer the workspace source over the separately packaged runtime.
import argparse
import csv
import json
import re
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from track_insight.core.errors import ContractError
from track_insight.directions.clustering import duplicate_components, overlapping_groups
from track_insight.directions.contracts import DirectionDedupOutput, DirectionGroupingOutput
from track_insight.directions.service import (
    DirectionDraft,
    _apply_merge_groups,
    _combine_exact_drafts,
    _validate_dedup_output,
    _validate_group_output,
)
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.settings import get_settings, load_poc_config

DEFAULT_TRACK_IDS = [
    "dtrack_industrial_automation_instruments",
    "dtrack_scientific_instruments_testing_equipment",
]


@dataclass(frozen=True)
class TopicUnit:
    ref: str
    topic_ids: list[str]
    name: str
    definition: str

    @property
    def embedding_text(self) -> str:
        return f"{self.name}\n{self.definition}"


def main() -> int:
    args = _arguments()
    output_dir = args.output.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    status_path = output_dir / "status.json"
    started_at = datetime.now(UTC).isoformat()
    _write_status(
        status_path,
        status="running",
        stage="loading_input",
        started_at=started_at,
        output_csv=str(output_dir / "directions.csv"),
    )
    try:
        dashboard = _load_dashboard(args.dashboard)
        topics, human_tracks, source_tracks, selected = _select_topics(dashboard, args.track_id)
        config = load_poc_config(args.config)
        settings = get_settings()
        if settings.bailian_api_key is None:
            raise RuntimeError("DASHSCOPE_API_KEY is not configured")
        units = _topic_units(topics)
        _write_json(
            output_dir / "input_manifest.json",
            {
                "selected_human_tracks": selected,
                "topic_count": len(topics),
                "topic_unit_count": len(units),
                "group_topics": config.directions.group_topics,
                "overlap_ratio": config.directions.overlap_ratio,
                "workers": args.workers,
                "model": config.directions.model,
                "embedding_model": config.directions.embedding_model,
            },
        )

        _write_status(
            status_path,
            status="running",
            stage="embedding_topics",
            started_at=started_at,
            topic_count=len(topics),
            topic_unit_count=len(units),
            workers=args.workers,
        )
        vectors, embedding_usage = _embed_parallel(
            units,
            config=config,
            api_key=settings.bailian_api_key.get_secret_value(),
            workers=args.workers,
        )
        groups = overlapping_groups(
            vectors,
            max_topics=config.directions.group_topics,
            overlap_ratio=config.directions.overlap_ratio,
        )
        _write_status(
            status_path,
            status="running",
            stage="grouping_directions",
            started_at=started_at,
            topic_count=len(topics),
            topic_unit_count=len(units),
            group_count=len(groups),
            workers=args.workers,
        )
        grouping_prompt = config.directions.prompts.grouping.read_text(encoding="utf-8")
        grouped = _group_parallel(
            units,
            groups,
            prompt=grouping_prompt,
            config=config,
            api_key=settings.bailian_api_key.get_secret_value(),
            workers=args.workers,
            output_dir=output_dir,
        )
        ref_to_unit = {unit.ref: unit for unit in units}
        drafts: list[DirectionDraft] = []
        grouping_usage: list[dict[str, Any]] = []
        for _, output, usage in sorted(grouped, key=lambda item: item[0]):
            grouping_usage.append(usage)
            for item in output.directions:
                drafts.append(
                    DirectionDraft(
                        name=item.name.strip(),
                        definition=item.definition.strip(),
                        topic_ids=_unique(
                            topic_id
                            for ref in _unique(item.topic_refs)
                            for topic_id in ref_to_unit[ref].topic_ids
                        ),
                    )
                )

        drafts = _combine_exact_drafts(drafts)
        _write_status(
            status_path,
            status="running",
            stage="deduplicating_directions",
            started_at=started_at,
            topic_count=len(topics),
            group_count=len(groups),
            direction_draft_count=len(drafts),
            workers=args.workers,
        )
        drafts, dedup_usage = _deduplicate_parallel(
            drafts,
            prompt=config.directions.prompts.deduplication.read_text(encoding="utf-8"),
            config=config,
            api_key=settings.bailian_api_key.get_secret_value(),
            workers=args.workers,
            output_dir=output_dir,
        )
        assigned = {str(topic_id) for draft in drafts for topic_id in draft.topic_ids}
        expected = set(topics)
        discarded = expected - assigned

        csv_path = output_dir / "directions.csv"
        _write_csv(
            csv_path,
            drafts,
            discarded,
            topics,
            human_tracks,
            source_tracks,
        )
        finished_at = datetime.now(UTC).isoformat()
        _write_status(
            status_path,
            status="completed",
            stage="completed",
            started_at=started_at,
            finished_at=finished_at,
            topic_count=len(topics),
            assigned_topic_count=len(assigned),
            discarded_topic_count=len(discarded),
            direction_count=len(drafts),
            group_count=len(groups),
            workers=args.workers,
            output_csv=str(csv_path),
            usage={
                "topic_embeddings": embedding_usage,
                "grouping": grouping_usage,
                "deduplication": dedup_usage,
            },
        )
        return 0
    except BaseException as error:
        _write_status(
            status_path,
            status="failed",
            stage="failed",
            started_at=started_at,
            finished_at=datetime.now(UTC).isoformat(),
            error_type=type(error).__name__,
            error=str(error),
            traceback=traceback.format_exc(),
        )
        raise


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dashboard", type=Path, default=ROOT / "docs/reference/track_dashboard.html"
    )
    parser.add_argument("--config", type=Path, default=ROOT / "config/poc.yaml")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "outputs/direction_sample_automation_science"
    )
    parser.add_argument("--track-id", action="append", default=None)
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 9))
    args = parser.parse_args()
    args.track_id = args.track_id or DEFAULT_TRACK_IDS
    return args


def _load_dashboard(path: Path) -> dict[str, Any]:
    html = path.read_text(encoding="utf-8")
    match = re.search(
        r'<script id="dashboard-data" type="application/json">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    if match is None:
        raise RuntimeError("dashboard-data JSON was not found")
    return json.loads(match.group(1))


def _select_topics(
    dashboard: dict[str, Any], selected_ids: list[str]
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, set[str]],
    dict[str, set[str]],
    list[dict[str, Any]],
]:
    tracks_by_id = {track["id"]: track for track in dashboard["tracks"]}
    topics: dict[str, dict[str, Any]] = {}
    human_tracks: dict[str, set[str]] = {}
    source_tracks: dict[str, set[str]] = {}
    selected = []
    for selected_id in selected_ids:
        track = tracks_by_id.get(selected_id)
        if track is None:
            raise RuntimeError(f"human track not found: {selected_id}")
        own_topics: set[str] = set()
        for source_id in track["source_ids"]:
            source = dashboard["sources"][source_id]
            for topic_id in source["topic_ids"]:
                topic = dashboard["topics"].get(topic_id)
                if topic is None:
                    raise RuntimeError(f"Topic missing from dashboard: {topic_id}")
                topics[topic_id] = topic
                own_topics.add(topic_id)
                human_tracks.setdefault(topic_id, set()).add(track["name"])
                source_tracks.setdefault(topic_id, set()).add(source["name"])
        selected.append(
            {
                "id": track["id"],
                "name": track["name"],
                "source_track_count": len(track["source_ids"]),
                "topic_count": len(own_topics),
            }
        )
    return topics, human_tracks, source_tracks, selected


def _topic_units(topics: dict[str, dict[str, Any]]) -> list[TopicUnit]:
    buckets: dict[tuple[str, str], list[str]] = {}
    for topic_id, topic in sorted(topics.items(), key=lambda item: (item[1]["label"], item[0])):
        key = (_normalized(topic["label"]), _normalized(topic["definition"]))
        buckets.setdefault(key, []).append(topic_id)
    return [
        TopicUnit(
            ref=f"T{index}",
            topic_ids=topic_ids,
            name=topics[topic_ids[0]]["label"][:200],
            definition=topics[topic_ids[0]]["definition"][:300],
        )
        for index, topic_ids in enumerate(buckets.values(), start=1)
    ]


def _embed_parallel(units, *, config, api_key: str, workers: int):
    batches = [units[index : index + 10] for index in range(0, len(units), 10)]

    def call(batch_index: int, batch):
        client = _client(config, api_key)
        try:
            result = client.embed_texts(
                [unit.embedding_text for unit in batch], model=config.directions.embedding_model
            )
            return batch_index, result
        finally:
            client.close()

    completed = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(call, index, batch) for index, batch in enumerate(batches)]
        for future in as_completed(futures):
            completed.append(future.result())
    completed.sort(key=lambda item: item[0])
    vectors = [vector for _, result in completed for vector in result["vectors"]]
    usage = [result.get("usage", {}) for _, result in completed]
    return vectors, usage


def _group_parallel(
    units, groups, *, prompt: str, config, api_key: str, workers: int, output_dir: Path
):
    calls_dir = output_dir / "calls"
    calls_dir.mkdir(parents=True, exist_ok=True)

    def call(group_index: int, group):
        group_refs = {units[index].ref for index in group.all_indices}
        payload = {
            "topics": [
                {
                    "topic_ref": units[index].ref,
                    "name": units[index].name,
                    "definition": units[index].definition,
                }
                for index in group.all_indices
            ],
        }
        client = _client(config, api_key)
        try:
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            attempts = []
            for attempt in range(1, 4):
                result = client.complete_structured(
                    messages=messages,
                    output_model=DirectionGroupingOutput,
                    model=config.directions.model,
                    max_tokens=config.directions.max_output_tokens,
                    enable_thinking=False,
                )
                attempts.append(
                    {
                        "attempt": attempt,
                        "request_id": result.request_id,
                        "usage": result.usage,
                    }
                )
                try:
                    _validate_group_output(result.output, group_refs)
                    break
                except ContractError as error:
                    if attempt == 3:
                        raise
                    messages.extend(
                        [
                            {
                                "role": "assistant",
                                "content": result.output.model_dump_json(),
                            },
                            {
                                "role": "user",
                                "content": (
                                    "上次结果引用了输入中不存在的 topic_ref。"
                                    "请仅使用输入 topics 中的 topic_ref 重新输出：" + str(error)
                                ),
                            },
                        ]
                    )
        finally:
            client.close()
        _write_json(
            calls_dir / f"group_{group_index:02d}.json",
            {
                "input": payload,
                "output": result.output.model_dump(mode="json"),
                "attempts": attempts,
                "request_id": result.request_id,
            },
        )
        return group_index, result.output, {"attempts": attempts}

    completed = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(call, index, group) for index, group in enumerate(groups, start=1)]
        for future in as_completed(futures):
            completed.append(future.result())
    return completed


def _deduplicate_parallel(
    drafts, *, prompt: str, config, api_key: str, workers: int, output_dir: Path
):
    if len(drafts) < 2:
        return drafts, {"candidate_components": 0, "calls": []}
    vectors, embedding_usage = _embed_parallel(
        drafts, config=config, api_key=api_key, workers=workers
    )
    components = duplicate_components(
        vectors, minimum_similarity=config.directions.deduplication_similarity
    )
    for component in components:
        if len(component) > config.directions.deduplication_batch_directions:
            raise RuntimeError("deduplication candidate component exceeds configured limit")

    def call(component_index: int, component):
        refs = {f"D{index + 1}": index for index in component}
        payload = {
            "directions": [
                {
                    "direction_ref": ref,
                    "name": drafts[index].name,
                    "definition": drafts[index].definition,
                }
                for ref, index in refs.items()
            ]
        }
        client = _client(config, api_key)
        try:
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            attempts = []
            for attempt in range(1, 4):
                result = client.complete_structured(
                    messages=messages,
                    output_model=DirectionDedupOutput,
                    model=config.directions.model,
                    max_tokens=config.directions.max_output_tokens,
                    enable_thinking=False,
                )
                attempts.append(
                    {
                        "attempt": attempt,
                        "request_id": result.request_id,
                        "usage": result.usage,
                    }
                )
                try:
                    _validate_dedup_output(result.output, set(refs))
                    break
                except ContractError as error:
                    if attempt == 3:
                        raise
                    messages.extend(
                        [
                            {
                                "role": "assistant",
                                "content": result.output.model_dump_json(),
                            },
                            {
                                "role": "user",
                                "content": "请修正未知或重复引用并重新输出完整结果：" + str(error),
                            },
                        ]
                    )
        finally:
            client.close()
        _write_json(
            output_dir / "calls" / f"dedup_{component_index:02d}.json",
            {
                "input": payload,
                "output": result.output.model_dump(mode="json"),
                "attempts": attempts,
                "request_id": result.request_id,
            },
        )
        groups = [
            (
                [refs[ref] for ref in group.direction_refs],
                group.name.strip(),
                group.definition.strip(),
            )
            for group in result.output.merge_groups
        ]
        return component_index, groups, {"attempts": attempts}

    completed = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(call, index, component)
            for index, component in enumerate(components, start=1)
        ]
        for future in as_completed(futures):
            completed.append(future.result())
    completed.sort(key=lambda item: item[0])
    merge_groups = [group for _, groups, _ in completed for group in groups]
    return _apply_merge_groups(drafts, merge_groups), {
        "candidate_components": len(components),
        "embedding": embedding_usage,
        "calls": [usage for _, _, usage in completed],
    }


def _write_csv(
    path: Path,
    drafts,
    discarded: set[str],
    topics: dict[str, dict[str, Any]],
    human_tracks: dict[str, set[str]],
    source_tracks: dict[str, set[str]],
) -> None:
    fields = [
        "row_type",
        "direction_name",
        "direction_definition",
        "topic_count",
        "topic_ids",
        "topic_names",
        "human_tracks",
        "original_subtracks",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for draft in sorted(drafts, key=lambda item: item.name):
            topic_ids = sorted(map(str, draft.topic_ids))
            writer.writerow(
                {
                    "row_type": "direction",
                    "direction_name": draft.name,
                    "direction_definition": draft.definition,
                    "topic_count": len(topic_ids),
                    "topic_ids": " | ".join(topic_ids),
                    "topic_names": " | ".join(topics[item]["label"] for item in topic_ids),
                    "human_tracks": " | ".join(
                        sorted({name for item in topic_ids for name in human_tracks[item]})
                    ),
                    "original_subtracks": " | ".join(
                        sorted({name for item in topic_ids for name in source_tracks[item]})
                    ),
                }
            )
        for topic_id in sorted(discarded):
            writer.writerow(
                {
                    "row_type": "discarded_topic",
                    "direction_name": "",
                    "direction_definition": "",
                    "topic_count": 1,
                    "topic_ids": topic_id,
                    "topic_names": topics[topic_id]["label"],
                    "human_tracks": " | ".join(sorted(human_tracks[topic_id])),
                    "original_subtracks": " | ".join(sorted(source_tracks[topic_id])),
                }
            )


def _client(config, api_key: str) -> BailianClient:
    return BailianClient(
        api_key=api_key,
        base_url=config.bailian.base_url,
        timeout_seconds=config.bailian.timeout_seconds,
        max_retries=config.bailian.max_retries,
    )


def _write_status(path: Path, **payload: Any) -> None:
    payload["updated_at"] = datetime.now(UTC).isoformat()
    _write_json(path, payload)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _normalized(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def _unique(values) -> list:
    return list(dict.fromkeys(values))


if __name__ == "__main__":
    raise SystemExit(main())
