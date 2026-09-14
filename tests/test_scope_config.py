from pathlib import Path

import pytest
from pydantic import ValidationError

from track_insight.scope_config import CollectionScope, load_collection_scope


def test_workspace_collection_scope() -> None:
    scope = load_collection_scope(Path("config/collection_scope.yaml"))

    assert scope.target_regions[0].code == "CN-11"
    assert scope.backfill_months == 24
    assert scope.document_formats == {"html", "pdf"}
    assert len(scope.fingerprint()) == 64


def test_search_provider_is_required_when_enabled() -> None:
    with pytest.raises(ValidationError, match="search_provider"):
        CollectionScope.model_validate(
            {
                "version": "test",
                "target_regions": [{"code": "CN-11", "name": "北京市"}],
                "backfill_months": 24,
                "document_formats": ["html"],
                "discovery": {"search_enabled": True},
            }
        )
