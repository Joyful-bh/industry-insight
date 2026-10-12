import json
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from track_insight.core.errors import ContractError
from track_insight.infrastructure.models import (
    Base,
    CandidateTrack,
    CandidateTrackAnalysis,
    ModelRun,
)
from track_insight.settings import load_poc_config
from track_insight.tracks.contracts import SmbAssociationOutput
from track_insight.tracks.service import TrackService, _load_smb_product_catalog


@pytest.fixture
def catalog_dir():
    with TemporaryDirectory(prefix="smb-catalog-") as directory:
        yield Path(directory)


@pytest.mark.parametrize("associations", [[], [
    {
        "workload": "工程设计与仿真",
        "product_category": "工作站",
        "reason": "机床研发可能涉及CAD设计",
    }
]])
def test_catalog_is_passed_to_actual_analysis_call_and_changes_cache_key(catalog_dir, associations):
    catalog = catalog_dir / "catalog.md"
    catalog.write_text("| 工作站 | CAD工程设计 |", encoding="utf-8")
    config = load_poc_config()
    config.stage4.smb_product_catalog = catalog
    calls = []

    class Client:
        def complete_structured(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                output=kwargs["output_model"].model_validate({"smb_value_analysis": associations}),
                request_id="test", usage={}, raw_response={}, recovery_used=False,
            )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        plan_id = uuid.uuid4()
        session.add(
            CandidateTrack(
                research_plan_id=plan_id,
                created_by_run_id=uuid.uuid4(),
                updated_by_run_id=uuid.uuid4(),
                canonical_key="machine",
                name="机床制造",
                definition="从事机床研发与制造的企业。",
                enterprise_archetype="机床制造企业",
                confidence=0.8,
                track_fingerprint="test",
            )
        )
        session.commit()
        service = TrackService(client=Client(), config=config)
        assert service.analyze(session, plan_id=plan_id)["completed"] == 1
        first = session.scalar(select(ModelRun)).input_fingerprint
        payload = json.loads(calls[0]["messages"][1]["content"])
        assert set(payload) == {"track", "smb_product_catalog"}
        assert payload["track"] == {
            "name": "机床制造", "definition": "从事机床研发与制造的企业。"
        }
        assert calls[0]["output_model"] is SmbAssociationOutput
        saved = session.scalar(select(CandidateTrackAnalysis))
        assert saved.smb_value_analysis == associations
        assert saved.signal_statistics == {} and saved.evidence_event_ids == []
        track = session.scalar(select(CandidateTrack))
        assert track.enterprise_archetype == "机床制造企业"
        assert service.analyze(session, plan_id=plan_id)["skipped"] == 1
        track.track_fingerprint = "changed-only-upstream-evidence"
        session.commit()
        assert service.analyze(session, plan_id=plan_id)["skipped"] == 1
        assert payload["smb_product_catalog"] == {
            "source": catalog.as_posix(),
            "content": "| 工作站 | CAD工程设计 |",
        }
        assert "不得将行业专用生产设备" in calls[0]["messages"][0]["content"]
        catalog.write_text("| 工作站 | CAD工程设计 |\n| 数据服务 | 数据备份 |", encoding="utf-8")
        service.analyze(session, plan_id=plan_id)
        fingerprints = list(session.scalars(select(ModelRun.input_fingerprint)))
        assert len(fingerprints) == 2
        assert fingerprints[1] != first
        assert "数据备份" in json.loads(calls[1]["messages"][1]["content"])[
            "smb_product_catalog"
        ]["content"]
    engine.dispose()


def test_missing_catalog_is_not_silently_ignored(catalog_dir):
    with pytest.raises(FileNotFoundError):
        _load_smb_product_catalog(catalog_dir / "missing.md")


def test_empty_catalog_is_rejected(catalog_dir):
    catalog = catalog_dir / "empty.md"
    catalog.write_text("\ufeff  \n", encoding="utf-8")
    with pytest.raises(ContractError, match="catalog is empty"):
        _load_smb_product_catalog(catalog)


@pytest.mark.parametrize("path", ["config/poc.yaml", "config/national.yaml"])
def test_formal_configs_load_existing_catalog(path):
    config = load_poc_config(Path(path))
    catalog = _load_smb_product_catalog(config.stage4.smb_product_catalog)
    assert "ThinkStation" in catalog["content"]
    assert "综合交付服务" in catalog["content"]
