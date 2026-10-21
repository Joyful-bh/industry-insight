import json
from types import SimpleNamespace

import pytest

from track_insight.core.errors import ContractError
from track_insight.tracks.contracts import IdentifiedTrackBuildOutput
from track_insight.tracks.preparation import (
    Observation,
    coalesce_compatible_names,
    expand_observation_output,
    group_output_model,
    prepare_observations,
    semantic_groups,
)


def observation(i):
    return Observation(f"O{i}", [i], {i}, {i}, set(), {"topic_ref": f"O{i}", "label": "x"})


def test_semantic_groups_cover_all_and_obey_both_limits():
    rows = [observation(i) for i in range(100)]
    groups = semantic_groups(rows, [[1, 0]] * 50 + [[0, 1]] * 50, max_topics=12, max_chars=350)
    assert sorted(o.ref for group in groups for o in group) == sorted(o.ref for o in rows)
    assert all(len(g) <= 12 for g in groups)
    assert all(len(json.dumps([o.payload for o in g], ensure_ascii=False)) <= 350 for g in groups)
    assert all(len({int(o.ref[1:]) >= 50 for o in g}) == 1 for g in groups)


def test_expand_restores_every_original_topic_and_rejects_missing_refs():
    a, b = observation(1), observation(2)
    a.topic_ids = [1, 3]
    output = IdentifiedTrackBuildOutput.model_validate(
        {
            "tracks": [
                {
                    "name": "显示面板制造",
                    "offering_family": "显示面板",
                    "supplier_role": "MANUFACTURE_PRODUCT",
                    "refs": ["O1"],
                    "supporting_refs": [],
                }
            ],
            "excluded_refs": ["O2"],
        }
    )
    expanded = expand_observation_output(output, [a, b], {"T1": 1, "T2": 2, "T3": 3})
    assert expanded.tracks[0].refs == ["T1", "T3"]
    assert expanded.excluded_refs == ["T2"]
    with pytest.raises(ContractError):
        expand_observation_output(output, [a, b, observation(4)], {"T1": 1, "T2": 2, "T3": 3})


def test_same_label_different_definitions_stay_separate_and_events_are_unique():
    topics = [
        SimpleNamespace(id=i, label="显示面板", definition=d)
        for i, d in [(1, "制造显示面板"), (2, "制造显示面板"), (3, "运营显示广告")]
    ]
    event = SimpleNamespace(
        id=10,
        page_analysis_id=20,
        regions=["北京"],
        summary="制造显示面板",
        title="面板投产",
        signal_date=None,
        event_status="observed",
        event_type="production",
        industry_objects=[],
    )

    class Session:
        def __init__(self):
            self.count = 0

        def execute(self, statement):
            self.count += 1
            return (
                [(1, event, 30), (2, event, 30), (3, event, 30)]
                if self.count == 1
                else [(10, "面板投产")]
            )

    rows = prepare_observations(Session(), topics)
    assert len(rows) == 2
    merged = next(o for o in rows if len(o.topic_ids) == 2)
    assert merged.payload["observation_count"] == 2
    assert merged.payload["unique_event_count"] == 1
    assert merged.payload["source_document_count"] == 1


def test_invalid_vectors_fail_before_generation():
    with pytest.raises(ContractError):
        semantic_groups([observation(1)], [[0, 0]])


def test_identity_must_reference_events_of_core_topics():
    rows = [observation(1), observation(2)]
    for i, row in enumerate(rows):
        row.payload["representative_events"] = [{"event_ref": f"E{i}"}]
    schema = group_output_model(rows)
    payload = {
        "tracks": [
            {
                "name": "显示面板制造",
                "offering_family": "显示面板",
                "supplier_role": "MANUFACTURE_PRODUCT",
                "refs": ["O1"],
                "supporting_refs": [],
                "core_event_refs": ["E1"],
            }
        ],
        "excluded_refs": ["O2"],
    }
    with pytest.raises(ValueError):
        schema.model_validate(payload)
    payload["tracks"][0]["core_event_refs"] = ["E0"]
    assert schema.model_validate(payload).tracks[0].core_event_refs == ["E0"]


def test_same_name_compaction_preserves_variants_counts_and_separate_boundaries():
    rows = [observation(i) for i in range(1, 4)]
    for i, o in enumerate(rows):
        o.payload.update(label="显示面板", definition=f"定义{i}", representative_events=[])
    rows[1].event_ids = rows[0].event_ids.copy()
    rows[1].source_ids = rows[0].source_ids.copy()
    compact, vectors = coalesce_compatible_names(rows, [[1, 0], [1, 0.1], [0, 1]])
    assert len(compact) == len(vectors) == 2
    assert compact[0].payload["observation_count"] == 2
    assert compact[0].payload["unique_event_count"] == 1
    assert compact[0].payload["source_document_count"] == 1
    assert len(compact[0].payload["definition_variants"]) == 2
    assert compact[1].topic_ids == [3]


def evidence_fixture():
    rows = [observation(1), observation(2)]
    for i, row in enumerate(rows, 1):
        row.payload["representative_events"] = [{"event_ref": f"E{i}"}]
    payload = {
        "tracks": [
            {
                "name": "显示面板制造",
                "offering_family": "显示面板",
                "supplier_role": "MANUFACTURE_PRODUCT",
                "refs": ["O1"],
                "supporting_refs": ["O2"],
                "core_event_refs": ["E1", "E2"],
            }
        ],
        "excluded_refs": [],
    }
    return rows, payload


def test_misplaced_evidence_is_removed_without_changing_assignment_or_raw_input(caplog):
    rows, payload = evidence_fixture()
    output = group_output_model(rows).model_validate(payload)
    assert output.tracks[0].core_event_refs == ["E1"]
    assert output.tracks[0].refs == ["O1"]
    assert output.tracks[0].supporting_refs == ["O2"]
    assert payload["tracks"][0]["core_event_refs"] == ["E1", "E2"]
    assert "removed misplaced core event refs" in caplog.text


@pytest.mark.parametrize("events", [[], ["E2"], ["E1", "fabricated"]])
def test_missing_or_unknown_identity_is_not_silently_accepted(events):
    rows, payload = evidence_fixture()
    payload["tracks"][0]["core_event_refs"] = events
    with pytest.raises(ValueError, match="allowed=.*E1"):
        group_output_model(rows).model_validate(payload)


def test_normalization_does_not_allow_shared_core_evidence():
    rows, payload = evidence_fixture()
    payload["tracks"].append({**payload["tracks"][0], "name": "另一面板制造"})
    with pytest.raises(ValueError, match="shared Topic requires distinct core event"):
        group_output_model(rows).model_validate(payload)


def test_group_contract_rejects_silently_omitted_references():
    schema = group_output_model([observation(1), observation(2)])
    with pytest.raises(ValueError, match="missing"):
        schema.model_validate({"tracks": [], "excluded_refs": ["O1"]})
    output = schema.model_validate({"tracks": [], "excluded_refs": ["O1", "O2"]})
    assert expand_observation_output(
        output, [observation(1), observation(2)], {"T1": 1, "T2": 2}
    ).excluded_refs == ["T1", "T2"]


def test_production_grouping_reuses_completed_calls(monkeypatch):
    import track_insight.tracks.service as service

    rows = [observation(1), observation(2)]
    monkeypatch.setattr(service, "prepare_observations", lambda s, t: rows)
    monkeypatch.setattr(service, "cached_embeddings", lambda *a: ([[1, 0], [0, 1]], {}))
    monkeypatch.setattr(service, "coalesce_compatible_names", lambda o, v: (o, v))
    monkeypatch.setattr(service, "semantic_groups", lambda *a, **k: [[rows[0]], [rows[1]]])
    monkeypatch.setattr(service, "_expand_topic_refs", lambda *a: "expanded")

    class Session:
        def __init__(self):
            self.calls = 0

        def scalar(self, statement):
            self.calls += 1
            return SimpleNamespace(
                structured_output={
                    "tracks": [],
                    "excluded_refs": [f"O{self.calls}"],
                }
            )

    config = SimpleNamespace(
        stage4=SimpleNamespace(
            grouping_embedding_model="text-embedding-v4",
            generation_batch_topics=40,
            generation_batch_chars=30000,
            model="test",
        )
    )
    session = Session()
    result, stats = service.TrackService(client=None, config=config)._build_semantic_groups(
        session,
        None,
        [SimpleNamespace(id=1), SimpleNamespace(id=2)],
        {"T1": 1, "T2": 2},
        {},
        "prompt",
        "v1",
    )
    assert result == "expanded" and stats["group_count"] == 2
    assert session.calls == 2


def test_production_group_failure_keeps_successful_call_and_does_not_publish(monkeypatch):
    import track_insight.tracks.service as service
    from track_insight.core.errors import ModelCallError

    rows = [observation(1), observation(2)]
    monkeypatch.setattr(service, "prepare_observations", lambda s, t: rows)
    monkeypatch.setattr(service, "cached_embeddings", lambda *a: ([[1, 0], [0, 1]], {}))
    monkeypatch.setattr(service, "coalesce_compatible_names", lambda o, v: (o, v))
    monkeypatch.setattr(service, "semantic_groups", lambda *a, **k: [[rows[0]], [rows[1]]])

    class Session:
        def __init__(self):
            self.saved = []

        def scalar(self, statement):
            return None

        def add(self, row):
            self.saved.append(row)

        def commit(self):
            pass

    class Client:
        def __init__(self):
            self.calls = 0

        def complete_structured(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise ModelCallError("second group failed")
            return SimpleNamespace(
                output=kwargs["output_model"].model_validate(
                    {
                        "tracks": [],
                        "excluded_refs": ["O1"],
                    }
                ),
                request_id="r1",
                usage={},
                raw_response={},
            )

    config = SimpleNamespace(
        stage4=SimpleNamespace(
            grouping_embedding_model="text-embedding-v4",
            generation_batch_topics=40,
            generation_batch_chars=30000,
            model="test",
            generation_max_output_tokens=8192,
        )
    )
    session = Session()
    with pytest.raises(ModelCallError):
        service.TrackService(client=Client(), config=config)._build_semantic_groups(
            session,
            SimpleNamespace(id=None),
            [SimpleNamespace(id=1), SimpleNamespace(id=2)],
            {"T1": 1, "T2": 2},
            {},
            "prompt",
            "v1",
        )
    assert session.saved[0].status == "completed"
    assert session.saved[1].status == "failed_terminal"
