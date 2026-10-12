import json
from pathlib import Path

import httpx
import pytest

from track_insight.core.errors import ModelCallError
from track_insight.discovery import opensearch
from track_insight.discovery.contracts import SearchReviewOutput
from track_insight.discovery.search import apply_review_constraints
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.infrastructure.bailian.contracts import WebSearchSource
from track_insight.settings import Settings, load_poc_config


def test_external_search_preserves_metadata_for_review_and_audit(monkeypatch):
    config = load_poc_config(Path("config/national.yaml"))
    config.search.queries_per_task = 2
    config.bailian.review_batch_size = 1
    settings = Settings(
        OPENSEARCH_API_KEY="search-test",
        OPENSEARCH_BASE_URL="http://search.example.invalid",
    )
    monkeypatch.setattr(opensearch, "get_settings", lambda: settings)
    client = BailianClient(api_key="model-test", base_url="https://model.example.invalid/v1")
    requests = []

    def respond(request):
        assert request.url.scheme == "https"
        assert request.headers["Authorization"] == "Bearer search-test"
        payload = json.loads(request.content)
        requests.append(payload)
        assert "site" not in payload
        assert payload["content_type"] == "snippet"
        assert payload["query_rewrite"] is False
        return httpx.Response(
            200,
            json={
                "request_id": payload["query"],
                "usage": {"ops-web-search-001/lite": 1},
                "result": {
                    "search_result": [
                        {
                            "title": "制造业项目投产",
                            "link": "https://government.example/project",
                            "snippet": "机器人制造项目正式投产，新增产能。",
                            "meta_info": {"publishedTime": "2026-05-01T00:00:00+08:00"},
                        }
                    ]
                },
            },
        )

    original_client = httpx.Client
    monkeypatch.setattr(
        opensearch.httpx,
        "Client",
        lambda **kw: original_client(**{**kw, "transport": httpx.MockTransport(respond)}),
    )
    reviews = []

    def model_call(endpoint, payload, *, operation):
        if "queries" in payload["messages"][0]["content"]:
            output = {"queries": ["制造业项目投产", "制造业项目开工"]}
        else:
            content = json.loads(payload["messages"][-1]["content"])
            reviews.append(content)
            assert "search_answer" not in content
            source = content["search_results"][0]
            assert source["title"] == "制造业项目投产"
            assert source["content"] == "机器人制造项目正式投产，新增产能。"
            assert source["publish_date"].startswith("2026-05-01")
            output = {
                "judgments": [
                    {
                        "refer": source["refer"],
                        "decision": "keep",
                        "url_type": "content_page",
                        "source_class": "government",
                        "reason": "具体制造业项目投产",
                    }
                ]
            }
        return {
            "id": "model-call",
            "model": config.bailian.model,
            "usage": {"input_tokens": 20, "output_tokens": 10},
            "choices": [{"message": {"content": json.dumps(output)}}],
        }

    monkeypatch.setattr(client, "_post_compatible", model_call)
    discovered = []
    result = opensearch.search_and_review(
        client=client,
        config=config,
        messages=[
            {"role": "system", "content": "Review sources"},
            {"role": "user", "content": json.dumps({"query": "制造业项目"})},
        ],
        output_model=SearchReviewOutput,
        on_sources_discovered=lambda sources: discovered.extend(sources),
    )
    assert len(requests) == 2
    assert len(result.web_search) == len(discovered) == len(reviews) == 1
    assert result.call_count == 4
    assert result.usage["input_tokens"] == 40
    assert result.usage["opensearch"]["estimated_search_cost_cny"] == 0.024
    assert len(result.raw_response["search_response"]["search_calls"]) == 2
    client.close()


def test_program_rejects_out_of_range_dates_and_search_entries():
    sources = [
        WebSearchSource(refer="old", link="https://example.com/policy", publish_date="2025-03-01"),
        WebSearchSource(refer="entry", link="https://example.com/search/news"),
        WebSearchSource(refer="unknown", link="https://example.com/new-project"),
    ]
    output = SearchReviewOutput.model_validate(
        {
            "judgments": [
                {
                    "refer": source.refer,
                    "decision": "keep",
                    "url_type": "content_page",
                    "source_class": "government",
                    "reason": "具体产业项目进展",
                }
                for source in sources
            ]
        }
    )
    corrections = apply_review_constraints(
        output,
        sources,
        {
            "start_date": "2025-10-09",
            "end_date": "2026-10-09",
        },
    )
    assert [judgment.decision for judgment in output.judgments] == ["drop", "drop", "keep"]
    assert output.judgments[1].url_type == "source_entry"
    assert len(corrections) == 2


def test_search_error_is_not_treated_as_empty_success(monkeypatch):
    settings = Settings(OPENSEARCH_API_KEY="test", OPENSEARCH_BASE_URL="https://example.invalid")
    monkeypatch.setattr(opensearch, "get_settings", lambda: settings)
    client = BailianClient(api_key="test", base_url="https://model.example.invalid/v1")
    monkeypatch.setattr(
        client,
        "_post_compatible",
        lambda *a, **kw: {
            "choices": [{"message": {"content": '{"queries":["制造业政策"]}'}}],
        },
    )
    original_client = httpx.Client
    monkeypatch.setattr(
        opensearch.httpx,
        "Client",
        lambda **kw: original_client(
            **{
                **kw,
                "transport": httpx.MockTransport(
                    lambda req: httpx.Response(
                        200, json={"code": "Denied", "message": "No search permission"}
                    )
                ),
            }
        ),
    )
    with pytest.raises(ModelCallError, match="no result object"):
        opensearch.search_and_review(
            client=client,
            config=load_poc_config(Path("config/national.yaml")),
            messages=[{"role": "user", "content": '{"query":"制造业政策"}'}],
            output_model=SearchReviewOutput,
        )
    client.close()
