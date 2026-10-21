import json

import httpx
import pytest

from track_insight.core.errors import ModelCallError
from track_insight.discovery.contracts import SearchReviewOutput
from track_insight.infrastructure.bailian.client import BailianClient


@pytest.mark.parametrize("failures", [1, 2, 3])
def test_transport_disconnect_has_bounded_retries(monkeypatch, failures):
    client = BailianClient(api_key="test", base_url="https://example.invalid/v1")
    attempts = []
    delays = []

    def post(endpoint, *, json):
        attempts.append((endpoint, json))
        if len(attempts) <= failures:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response")
        return httpx.Response(200, json={"id": "ok"})

    monkeypatch.setattr(client.compatible_client, "post", post)
    monkeypatch.setattr("track_insight.infrastructure.bailian.client.time.sleep", delays.append)
    try:
        if failures < 3:
            assert client._post_compatible("test", {"model": "test"}, operation="test") == {
                "id": "ok"
            }
        else:
            with pytest.raises(ModelCallError) as exc:
                client._post_compatible("test", {"model": "test"}, operation="test")
            assert exc.value.code == "network_error" and exc.value.retryable
            assert exc.value.diagnostics["attempts"] == 3
        assert len(attempts) == min(failures + 1, 3)
        assert delays == [1, 2][:min(failures, 2)]
        assert all(item == attempts[0] for item in attempts)
    finally:
        client.close()


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_error_does_not_use_transport_retry(monkeypatch, status):
    client = BailianClient(api_key="test", base_url="https://example.invalid/v1")
    attempts = []

    def post(endpoint, *, json):
        attempts.append(endpoint)
        return httpx.Response(status, text="error")

    monkeypatch.setattr(client.compatible_client, "post", post)
    try:
        with pytest.raises(ModelCallError) as exc:
            client._post_compatible("test", {}, operation="test")
        assert exc.value.code == f"http_{status}"
        assert len(attempts) == 1
    finally:
        client.close()


def test_keyboard_interrupt_is_not_retried(monkeypatch):
    client = BailianClient(api_key="test", base_url="https://example.invalid/v1")

    def post(endpoint, *, json):
        raise KeyboardInterrupt

    monkeypatch.setattr(client.compatible_client, "post", post)
    try:
        with pytest.raises(KeyboardInterrupt):
            client._post_compatible("test", {}, operation="test")
    finally:
        client.close()


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_structured_repair_receives_specific_validation_error(monkeypatch, repair_succeeds):
    from pydantic import BaseModel, model_validator

    class Output(BaseModel):
        event_ref: str

        @model_validator(mode="after")
        def check_reference(self):
            if self.event_ref != "E1":
                raise ValueError("unknown event reference E2; allowed=['E1']")
            return self

    client = BailianClient(api_key="test", base_url="https://example.invalid/v1")
    requests = []

    def post(path, payload, *, operation):
        requests.append(payload)
        event = "E1" if repair_succeeds and len(requests) == 2 else "E2"
        return {
            "id": "test",
            "model": "test",
            "usage": {},
            "choices": [{"message": {"content": json.dumps({"event_ref": event})}}],
        }

    monkeypatch.setattr(client, "_post_compatible", post)
    try:
        kwargs = dict(
            messages=[{"role": "user", "content": "source E1"}],
            output_model=Output,
            model="test",
            enable_thinking=False,
        )
        if repair_succeeds:
            result = client.complete_structured(**kwargs)
            assert result.output.event_ref == "E1"
            assert result.call_count == 2 and result.recovery_used
            assert "initial_response" in result.raw_response
        else:
            with pytest.raises(ModelCallError) as error:
                client.complete_structured(**kwargs)
            assert error.value.code == "invalid_model_output_after_recovery"
        assert len(requests) == 2
        instruction = requests[1]["messages"][-1]["content"]
        assert "unknown event reference E2; allowed=['E1']" in instruction
        assert "只修正格式" not in instruction
        assert requests[1]["enable_thinking"] is False
    finally:
        client.close()


def test_cited_search_selects_locally_without_review(monkeypatch):
    client = BailianClient(api_key="test", base_url="https://example.invalid/v1")
    operations = []
    response = {
        "id": "search-citations",
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "sources": [{"url": "https://example.com/a"}, {"url": "https://test.gov.cn/b"}]
                },
            },
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "产业项目投产。[2]", "annotations": []}
                ],
            },
        ],
        "usage": {"total_tokens": 123},
    }

    def post(path, payload, *, operation):
        operations.append(operation)
        return response

    monkeypatch.setattr(client, "_post_compatible", post)
    result = client.search_and_review(
        messages=[{"role": "user", "content": "制造业项目"}],
        output_model=SearchReviewOutput,
        model="test",
        review_batch_size=10,
        search_max_output_tokens=8192,
        cited_only=True,
    )
    assert operations == ["responses_web_search"]
    assert result.call_count == 1
    assert result.usage == {"total_tokens": 123}
    assert [j.decision.value for j in result.output.judgments] == ["drop", "keep"]
    assert result.output.judgments[1].source_class == "government"
    assert result.raw_response["review_responses"] == []
    assert len(result.web_search) == 2
    client.close()


def test_citation_annotations_use_urls_not_arbitrary_numbers():
    client = BailianClient(api_key="test", base_url="https://example.invalid/v1")
    result = client.select_cited_sources(
        response={
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "sources": [
                            {"url": "https://example.com/a"},
                            {"url": "https://example.com/b"},
                        ]
                    },
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "说明[1]",
                            "annotations": [
                                {"type": "url_citation", "url": "https://example.com/b"}
                            ],
                        }
                    ],
                },
            ]
        },
        output_model=SearchReviewOutput,
        model="test",
    )
    assert [j.decision.value for j in result.output.judgments] == ["drop", "keep"]
    client.close()


def test_dashscope_citation_numbers_precede_url_deduplication():
    client = BailianClient(api_key="test", base_url="https://example.invalid/v1")
    result = client.select_cited_sources(
        response={
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "sources": [
                            {"url": "https://example.com/a"},
                            {"url": "https://example.com/a"},
                            {"url": "https://example.com/b"},
                        ]
                    },
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "制造业投产信息[citation:3]。"}],
                },
            ]
        },
        output_model=SearchReviewOutput,
        model="test",
    )
    assert len(result.web_search) == 2
    assert [j.decision.value for j in result.output.judgments] == ["drop", "keep"]
    client.close()


def test_uncited_summary_is_not_a_successful_empty_selection():
    client = BailianClient(api_key="test", base_url="https://example.invalid/v1")
    with pytest.raises(ModelCallError) as error:
        client.select_cited_sources(
            response={
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {"sources": [{"url": "https://example.com/a"}]},
                    },
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "无引用的综述"}],
                    },
                ]
            },
            output_model=SearchReviewOutput,
            model="test",
        )
    assert error.value.code == "missing_search_citations"
    client.close()


def test_search_and_review_uses_responses_urls_then_structured_review(monkeypatch) -> None:
    client = BailianClient(
        api_key="test",
        base_url="https://example.invalid/compatible-mode/v1",
    )
    search_response = {
        "id": "search-request",
        "model": "qwen3.7-flash",
        "output": [
            {
                "type": "web_search_call",
                "status": "completed",
                "action": {
                    "type": "search",
                    "sources": [
                        {"type": "url", "url": "https://example.com/a"},
                        {"type": "url", "url": "https://example.com/b"},
                    ],
                },
            },
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "检索到一个制造业项目投产页面。"}],
            },
        ],
        "usage": {"total_tokens": 100},
    }
    operations: list[str] = []
    requests: list[tuple[str, dict]] = []
    discovered: list[str] = []

    def fake_compatible(endpoint, payload, *, operation):
        operations.append(operation)
        requests.append((endpoint, payload))
        if endpoint == "responses":
            return search_response
        review_input = json.loads(payload["messages"][-1]["content"])
        refer = review_input["required_refers"][0]
        return {
            "id": f"review-{refer}",
            "model": "qwen3.7-flash",
            "usage": {"total_tokens": 80},
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "judgments": [
                                    {
                                        "refer": refer,
                                        "decision": "keep",
                                        "url_type": "content_page",
                                        "source_class": "government",
                                        "reason": "包含具体制造业项目投产信息",
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
        }

    monkeypatch.setattr(client, "_post_compatible", fake_compatible)
    result = client.search_and_review(
        messages=[
            {"role": "system", "content": "判断相关性"},
            {"role": "user", "content": "北京制造业投产"},
        ],
        output_model=SearchReviewOutput,
        model="qwen3.7-flash",
        review_batch_size=1,
        search_max_output_tokens=8192,
        on_sources_discovered=lambda sources: discovered.extend(source.link for source in sources),
    )

    assert operations == [
        "responses_web_search",
        "structured_completion",
        "structured_completion",
    ]
    assert requests[0][0] == "responses"
    assert requests[0][1]["tools"] == [{"type": "web_search"}]
    assert requests[0][1]["tool_choice"] == {
        "type": "allowed_tools",
        "mode": "required",
        "tools": [{"type": "web_search"}],
    }
    assert requests[1][1]["enable_thinking"] is False
    assert requests[2][1]["enable_thinking"] is False
    assert result.call_count == 3
    assert len(result.web_search) == 2
    assert len(result.output.judgments) == 2
    assert discovered == ["https://example.com/a", "https://example.com/b"]
    assert result.web_search[0].link == "https://example.com/a"
    assert result.output.judgments[0].decision == "keep"


def test_web_extractor_uses_explicit_single_page_tool(monkeypatch) -> None:
    client = BailianClient(
        api_key="test",
        base_url="https://example.invalid/compatible-mode/v1",
    )
    requests = []

    def fake_compatible(endpoint, payload, *, operation):
        requests.append((endpoint, payload, operation))
        return {
            "id": "extract-1",
            "model": "qwen3.7-flash",
            "status": "completed",
            "usage": {"total_tokens": 50},
            "output": [
                {
                    "type": "web_extractor_call",
                    "status": "completed",
                    "urls": ["https://example.com/policy"],
                    "output": "正文政策内容，包含制造业支持措施。",
                }
            ],
        }

    monkeypatch.setattr(client, "_post_compatible", fake_compatible)
    result = client.extract_web_page(
        url="https://example.com/policy",
        goal="提取政策正文",
        model="qwen3.7-flash",
        max_output_tokens=4096,
        reasoning_effort="low",
    )

    assert requests[0][0] == "responses"
    assert requests[0][2] == "responses_web_extractor"
    assert requests[0][1]["tools"] == [
        {"type": "web_search"},
        {"type": "web_extractor"},
    ]
    assert requests[0][1]["tool_choice"]["tools"] == [{"type": "web_extractor"}]
    assert requests[0][1]["reasoning"] == {"effort": "low"}
    assert result.extracted_text == "正文政策内容，包含制造业支持措施。"
    assert result.request_id == "extract-1"


def test_pdf_ocr_reads_text_from_responses_output(monkeypatch) -> None:
    client = BailianClient(
        api_key="test",
        base_url="https://example.invalid/compatible-mode/v1",
    )
    requests = []

    def fake_compatible(endpoint, payload, *, operation):
        requests.append((endpoint, payload, operation))
        return {
            "id": "ocr-1",
            "model": "qwen3.5-ocr",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "扫描版 PDF 正文"}],
                }
            ],
        }

    monkeypatch.setattr(client, "_post_compatible", fake_compatible)
    result = client.extract_pdf_ocr(
        url="https://example.com/policy.pdf",
        model="qwen3.5-ocr",
        max_output_tokens=4096,
    )

    assert requests[0][2] == "responses_pdf_ocr"
    assert requests[0][1]["input"][0]["content"][0] == {
        "type": "input_file",
        "file_url": "https://example.com/policy.pdf",
    }
    assert requests[0][1]["ocr_options"] == {"task": "document_parsing"}
    assert result.extracted_text == "扫描版 PDF 正文"
