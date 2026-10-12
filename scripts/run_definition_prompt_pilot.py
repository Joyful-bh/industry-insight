"""Replay fixed historical evidence to compare one definition-prompt change."""

import csv
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.event_stage_supervisor import client, save
from scripts.run_track_full import usage_total
from track_insight.settings import load_poc_config
from track_insight.tracks.contracts import TrackDefinitionOutput

ROOT = Path("data/track_quality/definition_boundary_pilot/run_1")
OLD_LINE = "必要时用一句话区分相邻角色，例如设备制造与设施运营、芯片设计与晶圆制造。"
NEW_LINE = (
    "以正向经营活动描述企业身份。仅在可能混淆目标主体或供给角色时补充区分，"
    "不将相邻产品、技术路线或应用方向写成排除条件。"
)


def main():
    config = load_poc_config(Path("config/national.yaml"))
    prompt = config.stage4.prompts.track_definition.read_text(encoding="utf-8")
    assert NEW_LINE in prompt
    prompts = {"baseline": prompt.replace(NEW_LINE, OLD_LINE), "revised": prompt}
    batches = {
        str(n): json.loads(
            Path(
                f"data/track_quality/track_grouped_pilot/run_3/calls/definition_{n}.input.json"
            ).read_text(encoding="utf-8")
        )["payload"]
        for n in (8, 56)
    }
    save(ROOT / "inputs.json", batches)
    save(ROOT / "prompts.json", prompts)
    save(ROOT / "status.json", {"status": "running", "model": config.stage4.model})

    def replay(task):
        arm, batch_id = task
        path = ROOT / f"{arm}_{batch_id}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        active = client(config)
        try:
            result = active.complete_structured(
                messages=[
                    dict(role="system", content=prompts[arm]),
                    dict(role="user", content=json.dumps(batches[batch_id], ensure_ascii=False)),
                ],
                output_model=TrackDefinitionOutput,
                model=config.stage4.model,
                max_tokens=4096,
                enable_thinking=False,
            )
            rows = result.output.model_dump(mode="json")["definitions"]
            refs = [row["track_ref"] for row in rows]
            assert len(refs) == len(set(refs))
            assert set(refs) == {t["track_ref"] for t in batches[batch_id]["tracks"]}
            output = dict(
                arm=arm,
                batch_id=batch_id,
                definitions=rows,
                usage=usage_total(result.raw_response),
                call_count=result.call_count,
            )
            save(ROOT / f"{arm}_{batch_id}.response.json", result.raw_response)
            save(path, output)
            return output
        finally:
            active.close()

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(replay, [(a, b) for a in prompts for b in batches]))
        indexed = {
            (r["arm"], r["batch_id"]): {d["track_ref"]: d["definition"] for d in r["definitions"]}
            for r in results
        }
        with (ROOT / "候选定义对照.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["批次", "候选编号", "赛道名称", "原提示词定义", "新提示词定义"])
            for batch, payload in batches.items():
                for track in payload["tracks"]:
                    ref = track["track_ref"]
                    writer.writerow(
                        [
                            batch,
                            ref,
                            track["name"],
                            indexed["baseline", batch][ref],
                            indexed["revised", batch][ref],
                        ]
                    )
        save(
            ROOT / "status.json",
            dict(
                status="completed",
                model=config.stage4.model,
                selected_candidates=16,
                calls=sum(r["call_count"] for r in results),
                usage={
                    k: sum(r["usage"][k] for r in results)
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                },
            ),
        )
    except Exception as error:
        save(ROOT / "status.json", dict(status="failed", error=str(error)))
        raise


if __name__ == "__main__":
    main()
