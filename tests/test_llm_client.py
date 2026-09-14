import io
import json
import urllib.request

import pytest

from track_insight.enums import RelevanceLabel
from track_insight.llm_client import LlmClassificationError, LlmClient
from track_insight.relevance import RelevanceResult

RULE_RESULT = RelevanceResult(
    label=RelevanceLabel.POSSIBLY_RELEVANT,
    score=0.4,
    signal_types=[],
    industries=[],
    matched_positive_rules=[],
    matched_negative_rules=[],
    reason="规则无法判断",
    evidence=[],
)


class Response:
    def __init__(self, payload: dict) -> None:
        self.body = io.BytesIO(json.dumps(payload, ensure_ascii=False).encode())

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def read(self) -> bytes:
        return self.body.read()


def test_llm_client_validates_structured_response(monkeypatch: pytest.MonkeyPatch) -> None:
    model_output = {
        "label": "relevant",
        "score": 0.9,
        "signal_types": ["project_progress"],
        "industries": ["智能制造"],
        "reason": "文档包含明确项目投产事件。",
        "evidence": ["项目正式投产"],
    }
    api_payload = {"choices": [{"message": {"content": json.dumps(model_output)}}]}
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: Response(api_payload))
    client = LlmClient(base_url="https://llm.example/v1", api_key="secret", model="model")

    result = client.classify(
        title="项目动态",
        text="某企业宣布项目正式投产，并启动二期建设。",
        source_name="测试来源",
        source_type="industrial_park",
        rule_result=RULE_RESULT,
    )

    assert result.label == RelevanceLabel.RELEVANT
    assert result.evidence == ["项目正式投产"]


def test_llm_client_rejects_non_verbatim_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    model_output = {
        "label": "irrelevant",
        "score": 0.9,
        "signal_types": [],
        "industries": [],
        "reason": "这是一条普通的政务工作会议。",
        "evidence": ["输入中不存在的证据"],
    }
    api_payload = {"choices": [{"message": {"content": json.dumps(model_output)}}]}
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: Response(api_payload))
    client = LlmClient(base_url="https://llm.example/v1", api_key="secret", model="model")

    with pytest.raises(LlmClassificationError, match="exact excerpt"):
        client.classify(
            title="工作会议",
            text="有关单位召开会议。",
            source_name="测试来源",
            source_type="government_portal",
            rule_result=RULE_RESULT,
        )
