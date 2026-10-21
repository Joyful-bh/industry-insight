from datetime import date
from types import SimpleNamespace

from track_insight.discovery.coverage import build_coverage_audit
from track_insight.settings import load_poc_config


def test_audit_distinguishes_retained_content_and_unknown_dates():
    plan = SimpleNamespace(id="P", regions=["北京市", "江苏省"],
                           start_date=date(2025, 10, 9), end_date=date(2026, 10, 9))
    package = SimpleNamespace(id="W", status="completed")
    tasks = [SimpleNamespace(id="S1", target_regions=["北京市"], query="北京2026制造业政策",
                             status="completed", completion_reason=None),
             SimpleNamespace(id="S2", target_regions=["江苏省"], query="江苏2026制造业政策",
                             status="failed_terminal", completion_reason=None)]
    results = [SimpleNamespace(search_task_id="S1", decision="keep", url_type="content_page",
                               source_class="government", url="https://example.test/policy",
                               publish_date=None)]

    class Session:
        responses = iter([[package], tasks, results, []])

        def get(self, model, key):
            return plan

        def scalars(self, statement):
            return next(self.responses)

    audit = build_coverage_audit(Session(), plan.id, load_poc_config(), persist=False)
    assert audit.region_coverage["北京市"]["retained_content_urls"] == 1
    assert audit.region_coverage["北京市"]["unknown_publication_dates"] == 1
    assert audit.region_coverage["江苏省"]["failed_searches"] == 1
    assert audit.region_coverage["江苏省"]["supplement_reasons"] == ["no_retained_content"]
