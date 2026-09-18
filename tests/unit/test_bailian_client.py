import json

from track_insight.discovery.contracts import SearchReviewOutput
from track_insight.infrastructure.bailian.client import BailianClient


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
