import uuid
from pathlib import Path

import numpy as np
import pytest

from track_insight.tracks.bounded_markets import (
    _cluster_partition,
    comparison_packages,
    merge_packages,
    merge_packages_direct,
)
from track_insight.tracks.contracts import CandidateTrackOutput


def track(name, role="MANUFACTURE_PRODUCT"):
    return CandidateTrackOutput(
        name=name,
        offering_family=name,
        supplier_role=role,
        definition="从事相关产品持续研发生产的企业。",
        topic_ids=[uuid.uuid4()],
        identity_event_ids=[uuid.uuid4()],
        confidence=0.8,
        status="candidate",
    )


def test_packages_cover_recalled_edges_without_transitive_expansion():
    tracks = [track(f"产品方向{i}") for i in range(20)]
    vectors = np.random.default_rng(1).normal(size=(20, 16))
    packages, stats = comparison_packages(tracks, vectors, max_members=4)
    assert all(2 <= len(p) <= 4 for p in packages)
    assert stats["covered_edges"] == stats["recall_edges"]
    for a, b in stats["edges"]:
        assert any(a in p and b in p for p in packages)


def test_no_cross_role_recall():
    tracks = [track("显示面板"), track("显示面板", "DESIGN_PRODUCT")]
    packages, stats = comparison_packages(tracks, np.ones((2, 4)))
    assert packages == [] and stats["recall_edges"] == 0


def test_same_identity_pair_at_threshold_is_recalled():
    tracks = [track("服务器整机"), track("服务器整机")]
    packages, stats = comparison_packages(tracks, np.ones((2, 4)))
    assert packages == [[0, 1]]
    assert stats["covered_edges"] == 1


def test_cluster_partition_is_disjoint_complete_bounded_and_deterministic():
    tracks = [track("服务器整机") for _ in range(10)]
    vectors = np.ones((10, 4))
    packages, stats = comparison_packages(tracks, vectors, partitioning="cluster", max_members=3)
    assigned = [i for package in packages for i in package] + stats["singleton_ids"]
    assert sorted(assigned) == list(range(10))
    assert len(assigned) == len(set(assigned))
    assert max(map(len, packages)) <= 3
    assert stats["covered_edges"] + stats["cross_cluster_edges"] == stats["recall_edges"]
    repeated, _ = comparison_packages(tracks, vectors, partitioning="cluster", max_members=3)
    assert repeated == packages


def test_cluster_partition_does_not_join_classes_via_one_bridge():
    tracks = [track(f"候选{i}") for i in range(4)]
    adjacency = [
        {1: (.9, set())}, {0: (.9, set()), 2: (.8, set())},
        {1: (.8, set()), 3: (.7, set())}, {2: (.7, set())}
    ]
    groups = _cluster_partition(tracks, adjacency, 24, 18000)
    assert groups == [[0, 1], [2, 3]]


def test_cluster_partition_bounds_broad_hubs():
    tracks = [track(f"候选{i}") for i in range(4)]
    adjacency = [
        {1: (.9, set()), 2: (.8, set()), 3: (.7, set())},
        {0: (.9, set())}, {0: (.8, set())}, {0: (.7, set())}
    ]
    groups = _cluster_partition(tracks, adjacency, 3, 18000)
    assert max(map(len, groups)) <= 3
    assert sorted(i for group in groups for i in group) == list(range(4))


def test_cluster_partition_preserves_oversized_singletons_without_model_calls():
    tracks = [track("服务器整机"), track("服务器整机")]
    packages, stats = comparison_packages(
        tracks, np.ones((2, 4)), partitioning="cluster", max_chars=1
    )
    assert packages == [] and stats["singleton_ids"] == [0, 1]
    output, _ = merge_packages_direct(tracks, {}, packages, lambda *args: pytest.fail("No call"))
    assert output == tracks


def test_cluster_partition_keeps_roles_separate():
    tracks = [track("显示面板"), track("显示面板", "DESIGN_PRODUCT")]
    packages, stats = comparison_packages(tracks, np.ones((2, 4)), partitioning="cluster")
    assert packages == [] and stats["singleton_ids"] == [0, 1]


def test_route_ablation_and_quotas_are_observable():
    tracks = [track("服务器整机"), track("服务器整机"), track("乳制品加工")]
    vectors = np.array([[1, 0], [1, 0], [0, 1]])
    for route in ("lexical", "semantic"):
        _, stats = comparison_packages(
            tracks, vectors, recall_routes=(route,), lexical_top_k=1, semantic_top_k=1
        )
        assert stats["edges"] == [[0, 1]]
        assert stats["route_counts"] == {route: 1}


@pytest.mark.parametrize(
    "parameters",
    [
        {"recall_routes": ()},
        {"lexical_quantile": 1.1},
        {"semantic_top_k": 0},
    ],
)
def test_invalid_recall_parameters_fail(parameters):
    with pytest.raises(ValueError):
        comparison_packages([track("服务器")], [[1, 0]], **parameters)


@pytest.mark.parametrize("strategy", ["bounded", "bounded_direct"])
def test_formal_entry_uses_bounded_packages(monkeypatch, strategy):
    from types import SimpleNamespace

    from track_insight.tracks.contracts import TrackBuildOutput
    from track_insight.tracks.markets import MarketCatalog
    from track_insight.tracks.service import TrackService

    tracks = [track("服务器整机"), track("服务器整机")]
    config = SimpleNamespace(
        stage4=SimpleNamespace(
            market_merge_strategy=strategy,
            grouping_embedding_model="test",
            generation_max_output_tokens=16384,
            model="test",
        )
    )
    monkeypatch.setattr(
        "track_insight.tracks.service.cached_embeddings",
        lambda *a, **k: (np.ones((2, 4)), {"calls": 0, "input_tokens": 0}),
    )
    service = TrackService(client=None, config=config)
    seen = []

    def complete(session, pipeline, task, prompt, version, payload, schema, max_tokens):
        seen.append((task, max_tokens, payload))
        expected = (
            "track_market_direct_catalog"
            if strategy == "bounded_direct"
            else "track_market_catalog"
        )
        if task == expected:
            return MarketCatalog(markets=[])
        raise AssertionError("No audit needed for an empty proposal")

    monkeypatch.setattr(service, "_complete_cached_track_task", complete)
    output = TrackBuildOutput(tracks=tracks)
    stats = service._normalize_markets(None, None, output, {}, {"catalog": "c", "audit": "a"})
    assert len(output.tracks) == 2
    assert stats["recall"]["package_count"] == 1
    if strategy == "bounded_direct":
        assert stats["recall"]["assigned_candidates"] == 2
        assert stats["recall"]["singleton_candidates"] == 0
    assert seen[0][1] == 8192


@pytest.mark.parametrize("profile", ["config/poc.yaml", "config/national.yaml"])
def test_shipping_profiles_merge_with_original_c_prompt(monkeypatch, profile):
    from track_insight.settings import load_poc_config
    from track_insight.tracks.contracts import TrackBuildOutput
    from track_insight.tracks.service import TrackService

    config = load_poc_config(Path(profile))
    assert config.stage4.market_merge_strategy == "bounded_direct"
    prompt = config.stage4.prompts.track_market_catalog.read_text(encoding="utf-8")
    original = Path("experiments/track_quality/prompts/market_merge_simple_c.md")
    assert prompt == original.read_text(encoding="utf-8")
    monkeypatch.setattr(
        "track_insight.tracks.service.cached_embeddings",
        lambda *a, **k: (np.ones((2, 4)), {"calls": 0, "input_tokens": 0}),
    )
    service = TrackService(client=None, config=config)

    def complete(session, pipeline, task, actual_prompt, version, payload, schema, max_tokens):
        assert task == "track_market_direct_catalog"
        assert actual_prompt == prompt and max_tokens == 8192
        return schema.model_validate({"markets": [dict(
            market_ref="M1", name="服务器制造", offering_family="服务器",
            supplier_role="MANUFACTURE_PRODUCT",
            boundary="从事服务器整机研发生产制造的企业。",
            member_refs=[row["track_ref"] for row in payload["tracks"]],
        )]})

    monkeypatch.setattr(service, "_complete_cached_track_task", complete)
    output = TrackBuildOutput(tracks=[track("服务器整机"), track("服务器整机")])
    stats = service._normalize_markets(None, None, output, {}, {"catalog": prompt})
    assert len(output.tracks) == 1 and stats["model_audit_calls"] == 0


def test_conflicting_proposals_are_rejudged_not_union_merged():
    tracks = [track("服务器制造"), track("服务器整机"), track("服务器零部件")]
    calls = []

    def complete(task, payload, schema):
        calls.append((task, payload))
        if task == "audit":
            return schema.model_validate(
                {
                    "decisions": [
                        dict(group_ref=g["group_ref"], decision="ACCEPT") for g in payload["groups"]
                    ]
                }
            )
        refs = [t["track_ref"] for t in payload["tracks"]]
        members = refs if len(refs) == 2 else ["C0", "C1"]
        return schema.model_validate(
            {
                "markets": [
                    dict(
                        market_ref="M1",
                        name="服务器制造",
                        offering_family="服务器",
                        supplier_role="MANUFACTURE_PRODUCT",
                        boundary="从事服务器整机研发生产制造的企业。",
                        member_refs=members,
                    )
                ]
            }
        )

    result, stats = merge_packages(tracks, {}, [[0, 1], [1, 2]], complete)
    assert len(result) == 2 and stats["conflict_components"] == 1
    assert result[1] == tracks[2]
    assert set(result[0].topic_ids) == set(tracks[0].topic_ids + tracks[1].topic_ids)
    assert sum(task == "catalog" for task, _ in calls) == 3


def test_oversized_conflict_keeps_sources():
    tracks = [track("服务器整机" + str(i)) for i in range(4)]

    def complete(task, payload, schema):
        refs = [t["track_ref"] for t in payload["tracks"]]
        return schema.model_validate(
            {
                "markets": [
                    dict(
                        market_ref="M1",
                        name="服务器制造",
                        offering_family="服务器",
                        supplier_role="MANUFACTURE_PRODUCT",
                        boundary="从事服务器整机研发生产制造的企业。",
                        member_refs=refs,
                    )
                ]
            }
        )

    result, stats = merge_packages(tracks, {}, [[0, 1], [1, 2], [2, 3]], complete, max_members=3)
    assert result == tracks and stats["oversized_conflicts"] == [[0, 1, 2, 3]]


def test_direct_partition_can_share_mixed_source_without_audit():
    tracks = [track("机器人整机"), track("机器人零部件"), track("机器人整机及零部件")]

    def complete(task, payload, schema):
        assert task == "direct_catalog"
        return schema.model_validate(
            {
                "markets": [
                    dict(
                        market_ref="M1",
                        name="机器人整机制造",
                        offering_family="机器人整机",
                        supplier_role="MANUFACTURE_PRODUCT",
                        boundary="持续研发生产机器人整机的企业。",
                        member_refs=["C0", "C2"],
                    ),
                    dict(
                        market_ref="M2",
                        name="机器人零部件制造",
                        offering_family="机器人零部件",
                        supplier_role="MANUFACTURE_PRODUCT",
                        boundary="持续研发生产机器人零部件的企业。",
                        member_refs=["C1", "C2"],
                    ),
                ]
            }
        )

    result, stats = merge_packages_direct(tracks, {}, [[0, 1, 2]], complete)
    assert len(result) == 2 and stats["shared_source_candidates"] == 1
    assert stats["conflict_components"] == stats["model_audit_calls"] == 0
    assert tracks[2].topic_ids[0] in result[0].topic_ids
    assert tracks[2].topic_ids[0] in result[1].topic_ids


def test_direct_validator_allows_multiple_markets_from_mixed_sources():
    from track_insight.tracks.markets import DirectMarketCatalog, market_output_model

    payload = {"tracks": [{"track_ref": "C0"}, {"track_ref": "C1"}]}
    schema = market_output_model("direct_catalog", payload, DirectMarketCatalog)
    markets = [
        dict(
            market_ref=f"M{i}",
            name=f"机器人制造{i}",
            offering_family=f"机器人{i}",
            supplier_role="MANUFACTURE_PRODUCT",
            boundary="持续研发制造机器人产品的企业。",
            member_refs=["C0", "C1"],
        )
        for i in range(2)
    ]
    assert len(schema.model_validate({"markets": markets}).markets) == 2


def test_direct_exact_market_identity_coalesces_disjoint_packages():
    tracks = [track("服务器" + str(i)) for i in range(4)]

    def complete(task, payload, schema):
        return schema.model_validate(
            {
                "markets": [
                    dict(
                        market_ref="M1",
                        name="服务器制造",
                        offering_family="服务器",
                        supplier_role="MANUFACTURE_PRODUCT",
                        boundary="持续研发生产服务器整机的企业。",
                        member_refs=[row["track_ref"] for row in payload["tracks"]],
                    )
                ]
            }
        )

    result, stats = merge_packages_direct(tracks, {}, [[0, 1], [2, 3]], complete)
    assert len(result) == 1 and stats["consumed_candidates"] == 4
    assert set(result[0].topic_ids) == {t.topic_ids[0] for t in tracks}
