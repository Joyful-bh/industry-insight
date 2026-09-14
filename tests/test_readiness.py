import json

from track_insight.readiness import classify_readiness, write_readiness_reports


def test_classify_readiness() -> None:
    assert classify_readiness(fetched=2, failed=0, dated=2, snapshot=False, errors=[]) == "ready"
    assert classify_readiness(fetched=1, failed=0, dated=0, snapshot=False, errors=[]) == "limited"
    assert (
        classify_readiness(
            fetched=0, failed=1, dated=0, snapshot=False, errors=[{"error": "HTTP 403"}]
        )
        == "blocked"
    )
    assert (
        classify_readiness(
            fetched=0, failed=1, dated=0, snapshot=False, errors=[{"error": "empty"}]
        )
        == "invalid"
    )


def test_write_readiness_reports(workspace_tmp_path) -> None:
    report = {
        "generated_at": "2026-09-11T00:00:00+00:00",
        "source_count": 1,
        "summary": {"ready": 1, "limited": 0, "blocked": 0, "invalid": 0},
        "sources": [
            {
                "code": "example",
                "status": "ready",
                "pages_checked": 2,
                "documents_requested": 3,
                "attachments_fetched": 1,
                "date_status": "available",
                "pagination_status": "verified",
                "failed": 0,
                "errors": [],
            }
        ],
    }
    json_path, markdown_path = write_readiness_reports(report, workspace_tmp_path)
    assert json.loads(json_path.read_text(encoding="utf-8"))["source_count"] == 1
    assert "| example | ready |" in markdown_path.read_text(encoding="utf-8")
