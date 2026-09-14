from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from track_insight.cli import (
    _collection_resume_key,
    _legacy_batch_parameters_match,
    _migrate_legacy_completed_sources,
    _source_completion_matches,
)


def test_resume_key_changes_only_when_collection_parameters_change() -> None:
    arguments = {
        "mode": "backfill",
        "max_pages": 10,
        "max_items": 200,
        "stop_after_known": 20,
        "scope_fingerprint": "scope-v1",
    }

    original = _collection_resume_key(**arguments)
    assert original == _collection_resume_key(**arguments)
    assert original != _collection_resume_key(**{**arguments, "max_pages": 11})


def test_completed_source_is_skipped_only_when_config_is_unchanged() -> None:
    completed = {
        "source_a": {"config_version": "v1", "completed_at": "2026-09-14T00:00:00Z"}
    }

    assert _source_completion_matches(completed, "source_a", "v1")
    assert not _source_completion_matches(completed, "source_a", "v2")
    assert not _source_completion_matches(completed, "source_b", "v1")


def test_legacy_batch_matches_only_safe_backfill_defaults() -> None:
    batch = SimpleNamespace(
        input_config={
            "mode": "backfill",
            "max_pages": 10,
            "max_items": 200,
            "source_codes": ["source_a"],
            "cutoff": "2024-09-14T00:00:00+00:00",
        }
    )

    assert _legacy_batch_parameters_match(
        batch, mode="backfill", max_pages=10, max_items=200, stop_after_known=20
    )
    assert not _legacy_batch_parameters_match(
        batch, mode="incremental", max_pages=10, max_items=200, stop_after_known=20
    )


def test_legacy_migration_uses_completed_prefix_and_known_config_versions() -> None:
    created_at = datetime.now(UTC)
    batch = SimpleNamespace(
        id="legacy-run",
        created_at=created_at,
        input_config={"source_codes": ["source_a", "source_b", "source_c"]},
        counters={"next_source_index": 2},
    )

    class SessionStub:
        def scalar(self, _query: object) -> datetime:
            return created_at - timedelta(days=1)

    migrated = _migrate_legacy_completed_sources(
        SessionStub(), batch, {"source_a": "v1", "source_b": "v2", "source_c": "v3"}
    )

    assert set(migrated) == {"source_a", "source_b"}
    assert migrated["source_a"]["config_version"] == "v1"
