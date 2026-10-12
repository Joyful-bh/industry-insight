"""Redefine a frozen pilot pool using production evidence selection, without publication."""

import argparse
import csv
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sqlalchemy.orm import Session

from scripts.event_stage_supervisor import client, save
from scripts.run_track_full import usage_total
from track_insight.infrastructure.database import get_engine
from track_insight.infrastructure.models import Topic
from track_insight.settings import load_poc_config
from track_insight.tracks.contracts import CandidateTrackOutput
from track_insight.tracks.service import _definition_output_model, _topic_payload

ROOT = Path("data/track_quality/cluster_market_redefinition/run_1")
BASELINE = Path("data/track_quality/cluster_market_pilot/run_1/pilot.json")
SOURCE = Path("data/track_quality/track_full/run_5/defined_tracks.json")


def main():
    global ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT)
    parser.add_argument("--reuse", type=Path)
    args = parser.parse_args()
    ROOT = args.output
    config = load_poc_config(Path("config/national.yaml"))
    prompt = config.stage4.prompts.track_definition.read_text(encoding="utf-8")
    pilot = json.loads(BASELINE.read_text(encoding="utf-8"))
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    tracks = [CandidateTrackOutput.model_validate(row) for row in source["tracks"]]
    ids = list(range(len(tracks))) if args.all else pilot["selected_ids"]
    reused = {}
    if args.reuse:
        snapshot = json.loads((args.reuse / "prompt_snapshot.json").read_text(encoding="utf-8"))
        if snapshot != dict(definition=prompt, model=config.stage4.model):
            raise ValueError("Reuse prompt/model mismatch")
        previous_inputs = json.loads((args.reuse / "inputs.json").read_text(encoding="utf-8"))
        previous_tracks = json.loads(
            (args.reuse / "defined_tracks.json").read_text(encoding="utf-8")
        )["tracks"]
        for payload in previous_inputs:
            for row in payload["tracks"]:
                ref = row["track_ref"]
                i = int(ref[1:])
                original = tracks[i].model_dump(mode="json")
                prior = previous_tracks[i]
                if {k: v for k, v in original.items() if k != "definition"} != {
                    k: v for k, v in prior.items() if k != "definition"
                }:
                    raise ValueError(f"{ref}: reuse source mismatch")
                reused[ref] = prior["definition"]
    pending_ids = [i for i in ids if f"C{i}" not in reused]
    scope = dict(
        industry_scopes=config.scope.industry_scopes,
        track_boundary=config.stage4.target_track_boundary,
    )
    save(ROOT / "status.json", dict(status="running", stage="preparing", selected=len(ids)))
    snapshot_path = ROOT / "prompt_snapshot.json"
    snapshot = dict(definition=prompt, model=config.stage4.model)
    if snapshot_path.exists() and json.loads(snapshot_path.read_text(encoding="utf-8")) != snapshot:
        raise ValueError("Resume prompt/model mismatch; use a new output directory")
    save(snapshot_path, snapshot)
    input_path = ROOT / "inputs.json"
    if input_path.exists():
        payloads = json.loads(input_path.read_text(encoding="utf-8"))
        if {r["track_ref"] for p in payloads for r in p["tracks"]} != {
            f"C{i}" for i in pending_ids
        }:
            raise ValueError("Resume input selection mismatch")
    else:
        payloads = []
        engine = get_engine()
        try:
            with Session(engine) as session:
                for start in range(0, len(pending_ids), 8):
                    rows = []
                    for i in pending_ids[start : start + 8]:
                        track = tracks[i]
                        topics = []
                        if not track.identity_event_ids:
                            raise ValueError(f"C{i}: missing identity evidence")
                        for topic_id in track.topic_ids:
                            topic = session.get(Topic, topic_id)
                            if topic is None:
                                raise ValueError(f"C{i}: missing Topic {topic_id}")
                            topics.append(
                                _topic_payload(
                                    session, topic, identity_event_ids=track.identity_event_ids
                                )
                            )
                        if not any(t["representative_events"] for t in topics):
                            raise ValueError(f"C{i}: no matching core events")
                        rows.append(dict(track_ref=f"C{i}", name=track.name, topics=topics))
                    payloads.append(dict(scope=scope, tracks=rows))
        finally:
            engine.dispose()
        save(input_path, payloads)

    def redefine(pair):
        index, payload = pair
        path = ROOT / f"definition_{index}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        refs = {row["track_ref"]: tracks[int(row["track_ref"][1:])] for row in payload["tracks"]}
        active = client(config)
        try:
            result = active.complete_structured(
                messages=[
                    dict(role="system", content=prompt),
                    dict(role="user", content=json.dumps(payload, ensure_ascii=False)),
                ],
                output_model=_definition_output_model(refs),
                model=config.stage4.model,
                max_tokens=4096,
                enable_thinking=False,
            )
            save(ROOT / f"definition_{index}.response.json", result.raw_response)
            output = dict(
                definitions=result.output.model_dump(mode="json")["definitions"],
                usage=usage_total(result.raw_response),
                calls=result.call_count,
            )
            save(path, output)
            return output
        finally:
            active.close()

    try:
        save(
            ROOT / "status.json", dict(status="running", stage="definitions", batches=len(payloads))
        )
        results = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(redefine, pair) for pair in enumerate(payloads)]
            for future in as_completed(futures):
                results.append(future.result())
                save(
                    ROOT / "status.json",
                    dict(
                        status="running",
                        stage="definitions",
                        batches=len(payloads),
                        completed_batches=len(results),
                        reused_candidates=len(reused),
                        completed_candidates=len(reused)
                        + sum(len(r["definitions"]) for r in results),
                    ),
                )
        definitions = dict(reused)
        definitions.update(
            {d["track_ref"]: d["definition"] for r in results for d in r["definitions"]}
        )
        assert set(definitions) == {f"C{i}" for i in ids}
        with (ROOT / "候选定义对照.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["候选编号", "赛道名称", "历史定义", "新定义"])
            for i in ids:
                writer.writerow(
                    [f"C{i}", tracks[i].name, tracks[i].definition, definitions[f"C{i}"]]
                )
                tracks[i].definition = definitions[f"C{i}"]
        source["tracks"] = [t.model_dump(mode="json") for t in tracks]
        save(ROOT / "defined_tracks.json", source)
        save(
            ROOT / "status.json",
            dict(
                status="completed",
                selected=len(ids),
                reused_candidates=len(reused),
                batches=len(payloads),
                calls=sum(r["calls"] for r in results),
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
