"""Validate national search planning without executing searches or changing the database."""

from datetime import UTC, datetime
from pathlib import Path

import recover_topics_regression as replay

from track_insight.infrastructure.bailian.contracts import ModelRunResult
from track_insight.planning.contracts import (
    CompactPlanningOutput,
    ResearchScope,
    expand_planning_output,
)
from track_insight.planning.coverage import (
    expand_scope,
    generate_covered_plan,
    missing_regions,
    time_windows,
    validate_supplement,
)
from track_insight.settings import load_poc_config

replay.ROOT = Path("data/planning_quality/national_coverage/run_3_compact")


def main():
    config = load_poc_config(Path("config/national.yaml"))
    replay.MODEL = config.bailian.model
    replay.STATE["model"] = replay.MODEL
    replay.write(replay.ROOT / "status.json", replay.STATE)
    scope = expand_scope(ResearchScope.model_validate(config.scope.model_dump()))
    prompt = config.prompts.planning.read_text(encoding="utf-8")

    def complete(name, payload):
        compact = replay.call(name, payload, prompt, CompactPlanningOutput)
        output = expand_planning_output(
            compact, ResearchScope.model_validate(payload["scope"]), payload["region_refs"]
        )
        stored = replay.read(replay.ROOT / f"calls/responses/{name}.json")
        return ModelRunResult(
            model=replay.MODEL,
            output=output,
            usage=stored["usage"],
            raw_response=stored["raw_response"],
        )

    output, results = generate_covered_plan(scope, config, complete)
    replay.write(replay.ROOT / "plan.json", output.model_dump(mode="json"))
    tasks = [t for p in output.work_packages for t in p.search_tasks]
    supplement_regions = ["安徽省", "青海省"]
    supplement_payload = {
        "region_refs": {f"G{i}": region for i, region in enumerate(supplement_regions, 1)},
        "scope": scope.model_copy(update={"regions": supplement_regions}).model_dump(mode="json"),
        "region_coverage": {
            "安徽省": {"supplement_reasons": ["no_government_content"]},
            "青海省": {"supplement_reasons": ["no_retained_content"]},
        },
        "existing_queries": [t.query for t in tasks if t.target_regions[0] in supplement_regions],
        "test_type": "synthetic_coverage_gap; no searches have been executed",
    }
    supplement_prompt = config.prompts.planning_supplement.read_text(encoding="utf-8")
    compact = replay.call(
        "supplement_test", supplement_payload, supplement_prompt, CompactPlanningOutput
    )
    supplement = expand_planning_output(
        compact,
        ResearchScope.model_validate(supplement_payload["scope"]),
        supplement_payload["region_refs"],
    )
    validate_supplement(
        supplement,
        supplement_regions,
        supplement_payload["existing_queries"],
        scope.start_date.year,
        scope.end_date.year,
    )
    replay.write(replay.ROOT / "supplement_plan.json", supplement.model_dump(mode="json"))
    replay.write(
        replay.ROOT / "metrics.json",
        {
            "model": replay.MODEL,
            "scope": scope.model_dump(mode="json"),
            "regions": len(scope.regions),
            "work_packages": len(output.work_packages),
            "search_tasks": len(tasks),
            "missing_regions": missing_regions(output, scope.regions),
            "required_time_segments": time_windows(scope),
            "searches_executed": 0,
            "database_modified": False,
            "synthetic_supplement_queries": 4,
            "api_calls": len(list((replay.ROOT / "calls/responses").glob("*.json"))),
            "per_region": {p.regions[0]: len(p.search_tasks) for p in output.work_packages},
        },
    )
    replay.STATE["status"] = "completed"
    replay.STATE["finished_at"] = datetime.now(UTC).isoformat()
    replay.write(replay.ROOT / "status.json", replay.STATE)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        replay.STATE["status"] = "failed"
        replay.STATE["error"] = str(error)
        replay.write(replay.ROOT / "status.json", replay.STATE)
        raise
