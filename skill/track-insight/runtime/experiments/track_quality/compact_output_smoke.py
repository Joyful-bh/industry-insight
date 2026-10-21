"""Two parallel, bounded API checks; no searches or production database writes."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import recover_topics_regression as replay

from track_insight.infrastructure.bailian.contracts import ModelRunResult
from track_insight.planning.contracts import (
    CompactPlanningOutput,
    ResearchScope,
    expand_planning_output,
)
from track_insight.planning.coverage import generate_covered_plan
from track_insight.settings import load_poc_config
from track_insight.topics.contracts import CompactTopicGenerationOutput, expand_topic_output


def main():
    config = load_poc_config(Path("config/national.yaml"))
    replay.ROOT = Path("data/model_output_quality/compact_v2")
    replay.MODEL = config.bailian.model
    replay.STATE["model"] = replay.MODEL
    replay.write(replay.ROOT / "status.json", replay.STATE)

    def topics():
        raw = replay.read(Path("data/track_quality/snapshot.json"))["event"][:20]
        refs = {f"E{i}": uuid.UUID(e["id"]) for i, e in enumerate(raw, 1)}
        fields = [
            "event_type",
            "event_status",
            "title",
            "summary",
            "signal_date",
            "regions",
            "industries",
            "industry_objects",
            "topic_hint",
            "entities",
            "chain_roles",
            "smb_relevance",
            "confidence",
        ]
        payload = {
            "work_package": {
                "objective": "调查制造业政策与产业化信号",
                "regions": ["北京市"],
                "industry_scopes": ["制造业"],
            },
            "events": [
                {"event_ref": f"E{i}", **{k: e.get(k) for k in fields}}
                for i, e in enumerate(raw, 1)
            ],
        }
        compact = replay.call(
            "compact_topic_smoke",
            payload,
            config.stage3.prompts.topic_generation.read_text(encoding="utf-8"),
            CompactTopicGenerationOutput,
        )
        full = expand_topic_output(compact, refs)
        replay.write(replay.ROOT / "topics.json", full.model_dump(mode="json"))
        return {
            "events": len(raw),
            "topics": len(full.candidates),
            "all_refs_valid": True,
            "compact_json_characters": len(compact.model_dump_json()),
            "expanded_json_characters": len(full.model_dump_json()),
        }

    def planning():
        scope = ResearchScope.model_validate(config.scope.model_dump()).model_copy(
            update={"regions": ["江苏省", "青海省"]}
        )
        prompt = config.prompts.planning.read_text(encoding="utf-8")

        def complete(name, payload):
            compact = replay.call(
                "compact_planning_" + name, payload, prompt, CompactPlanningOutput
            )
            full = expand_planning_output(
                compact, ResearchScope.model_validate(payload["scope"]), payload["region_refs"]
            )
            stored = replay.read(replay.ROOT / f"calls/responses/compact_planning_{name}.json")
            return ModelRunResult(
                model=replay.MODEL,
                output=full,
                usage=stored["usage"],
                raw_response=stored["raw_response"],
            )

        full, _ = generate_covered_plan(scope, config, complete)
        replay.write(replay.ROOT / "plan.json", full.model_dump(mode="json"))
        return {
            "regions": 2,
            "work_packages": len(full.work_packages),
            "queries": sum(len(p.search_tasks) for p in full.work_packages),
            "validated": True,
        }

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {"topics": pool.submit(topics), "planning": pool.submit(planning)}
            metrics = {name: future.result() for name, future in futures.items()}
        replay.write(replay.ROOT / "metrics.json", metrics)
        replay.STATE["status"] = "completed"
        print(metrics)
    except Exception as error:
        replay.STATE["status"] = "failed"
        replay.STATE["error"] = str(error)
        raise
    finally:
        replay.STATE["finished_at"] = datetime.now(UTC).isoformat()
        replay.write(replay.ROOT / "status.json", replay.STATE)


if __name__ == "__main__":
    main()
