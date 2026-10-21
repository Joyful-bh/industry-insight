"""Frozen-input candidate instruction A/B replay; no DB writes or publication."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from scripts.event_stage_supervisor import client, save
from track_insight.core.fingerprints import fingerprint
from track_insight.settings import load_poc_config
from track_insight.tracks.preparation import Observation, group_output_model


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def schema_for(payload):
    return group_output_model(
        [Observation(o["topic_ref"], [], set(), set(), set(), o) for o in payload["topics"]]
    )


def response_usage(raw):
    recovery = raw.get("repair_response", raw.get("recovery_response"))
    responses = (
        [raw["initial_response"], recovery]
        if "initial_response" in raw and recovery is not None
        else [raw]
    )
    return {
        key: sum(r.get("usage", {}).get(key, 0) for r in responses)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--call-api", action="store_true")
    args = parser.parse_args()
    base = Path("data/track_quality/evidence_selection_replay/run_1")
    prompt = Path("experiments/track_quality/prompts/candidate_fact_first_v1.md").read_text(
        encoding="utf-8"
    )
    config = load_poc_config(Path("config/national.yaml"))
    config.stage4.model = read(base / "status.json")["model"]
    config.bailian.max_retries = 0
    jobs = []
    for index in (0, 3, 4):
        frozen = read(base / "calls" / f"revised_{index}.input.json")
        for arm in ("baseline", "fact_first"):
            instructions = frozen["prompt"] if arm == "baseline" else prompt
            name = f"{arm}_{index}"
            payload = frozen["payload"]
            schema = schema_for(payload)
            signature = fingerprint(
                config.stage4.model, instructions, payload, schema.model_json_schema(),
                config.stage4.generation_max_output_tokens, False,
            )
            record = dict(prompt=instructions, payload=payload, signature=signature)
            path = args.output / "calls" / f"{name}.input.json"
            if path.exists() and read(path) != record:
                raise RuntimeError("Frozen replay changed; use a new output directory")
            save(path, record)
            jobs.append((name, record, schema))
    protocol = dict(
        hypothesis="候选任务的互相牵制规则导致明确制造事实被 Topic 总身份盖过",
        variable="只改变 system 提示词；保持事实、Topic、分组、顺序、模型和 Schema",
        limits=dict(logical_calls=6, maximum_requests_with_recovery=12, concurrency=4),
        acceptance=[
            "O4 模具/工件和 O49 制糖的明确制造方向恢复，不能变成软件或设备供应商",
            "原有电力设备、传感器、海工装备、豆制品、PCB、芯片设计、正极材料、电解铝保留",
            "运营、融资、平台等材料不得仅因应用或关联而推导产品供应商",
            "不得把混合产业链作为一种产品，名称和引用不得超出事件事实",
            "结构完整；只增加候选数量不算通过；失败不进行提示词反复拟合",
        ],
        caveat="已知诊断样本，不是独立全国留出集；仅候选判定，不是全链路质量验收",
        production_modified=False,
    )
    protocol_path = args.output / "protocol.json"
    if protocol_path.exists() and read(protocol_path) != protocol:
        raise RuntimeError("Acceptance protocol changed; use a new output directory")
    save(protocol_path, protocol)
    state = dict(
        status="prepared", model=config.stage4.model, calls={},
        logical_call_limit=6, maximum_requests_with_recovery=12,
        started_at=datetime.now(UTC).isoformat(), database_modified=False,
    )
    save(args.output / "status.json", state)
    print(json.dumps(state, ensure_ascii=False), flush=True)
    if not args.call_api:
        return

    def run(job):
        name, record, schema = job
        path = args.output / "calls" / f"{name}.json"
        if path.exists():
            cached = read(path)
            if cached["signature"] != record["signature"]:
                raise RuntimeError("Cached result has different inputs")
            schema.model_validate(cached["output"])
            return cached, True
        active = client(config)
        try:
            result = active.complete_structured(
                messages=[
                    {"role": "system", "content": record["prompt"]},
                    {"role": "user", "content": json.dumps(record["payload"], ensure_ascii=False)},
                ],
                output_model=schema, model=config.stage4.model,
                max_tokens=config.stage4.generation_max_output_tokens, enable_thinking=False,
            )
        finally:
            active.close()
        saved = dict(
            signature=record["signature"], output=result.output.model_dump(mode="json"),
            usage=response_usage(result.raw_response), call_count=result.call_count,
        )
        save(path, saved)
        return saved, False

    state["status"] = "running"
    save(args.output / "status.json", state)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(run, job): job[0] for job in jobs}
        for future in as_completed(futures):
            name = futures[future]
            try:
                saved, reused = future.result()
                state["calls"][name] = dict(
                    status="completed", usage=saved["usage"], call_count=saved["call_count"],
                    reused=reused,
                )
            except Exception as exc:
                state["calls"][name] = dict(status="failed", error=str(exc))
                if hasattr(exc, "diagnostics"):
                    save(args.output / "calls" / f"{name}.failure.json", exc.diagnostics)
                    diagnostics = exc.diagnostics
                    if "initial_response" in diagnostics:
                        state["calls"][name]["usage"] = response_usage(diagnostics)
                        state["calls"][name]["call_count"] = (
                            2 if "recovery_response" in diagnostics
                            or "repair_response" in diagnostics else 1
                        )
            save(args.output / "status.json", state)
            print(name, state["calls"][name]["status"], flush=True)
    state["status"] = (
        "completed" if all(c["status"] == "completed" for c in state["calls"].values())
        else "failed"
    )
    state["finished_at"] = datetime.now(UTC).isoformat()
    save(args.output / "status.json", state)


if __name__ == "__main__":
    main()
