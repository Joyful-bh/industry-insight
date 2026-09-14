import json
import urllib.error
import urllib.request
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from track_insight.enums import RelevanceLabel
from track_insight.relevance import RelevanceResult

PROMPT_VERSION = "relevance-semantic-v1"


class LlmClassificationError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class SemanticRelevanceOutput(BaseModel):
    label: Literal["relevant", "possibly_relevant", "irrelevant"]
    score: float = Field(ge=0, le=1)
    signal_types: list[str] = Field(default_factory=list, max_length=12)
    industries: list[str] = Field(default_factory=list, max_length=12)
    reason: str = Field(min_length=5, max_length=500)
    evidence: list[str] = Field(default_factory=list, max_length=3)


class LlmClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: int = 60,
        max_input_chars: int = 12000,
    ) -> None:
        self.endpoint = _completion_endpoint(base_url)
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_input_chars = max_input_chars

    def classify(
        self,
        *,
        title: str | None,
        text: str,
        source_name: str,
        source_type: str,
        rule_result: RelevanceResult,
    ) -> RelevanceResult:
        source_text = text[: self.max_input_chars]
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "source_name": source_name,
                            "source_type": source_type,
                            "title": title or "",
                            "text": source_text,
                            "rule_assessment": {
                                "label": rule_result.label.value,
                                "signal_types": rule_result.signal_types,
                                "industries": rule_result.industries,
                                "reason": rule_result.reason,
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            retryable = error.code == 429 or error.code >= 500
            raise LlmClassificationError(
                f"LLM HTTP {error.code}", retryable=retryable
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise LlmClassificationError(f"LLM request failed: {error}", retryable=True) from error
        except json.JSONDecodeError as error:
            raise LlmClassificationError("LLM API returned invalid JSON") from error

        output = _validate_response(response_payload, source_text)
        return RelevanceResult(
            label=RelevanceLabel(output.label),
            score=output.score,
            signal_types=output.signal_types,
            industries=output.industries,
            matched_positive_rules=rule_result.matched_positive_rules,
            matched_negative_rules=rule_result.matched_negative_rules,
            reason=output.reason,
            evidence=output.evidence,
        )


def _validate_response(payload: dict, source_text: str) -> SemanticRelevanceOutput:
    try:
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("message content is not text")
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.removeprefix("```json").removeprefix("```")
            cleaned = cleaned.removesuffix("```").strip()
        output = SemanticRelevanceOutput.model_validate_json(cleaned)
    except (KeyError, IndexError, TypeError, ValidationError, json.JSONDecodeError) as error:
        raise LlmClassificationError(f"invalid structured LLM response: {error}") from error
    if output.label != "possibly_relevant" and not output.evidence:
        raise LlmClassificationError("decisive LLM result must contain evidence")
    if any(evidence not in source_text for evidence in output.evidence):
        raise LlmClassificationError("LLM evidence is not an exact excerpt from the document")
    return output


def _completion_endpoint(base_url: str) -> str:
    value = base_url.rstrip("/")
    return value if value.endswith("/chat/completions") else f"{value}/chat/completions"


def _system_prompt() -> str:
    return (
        "你负责判断材料是否包含可形成政策、产业、项目、企业经营或市场变化事件的信息。"
        "不要要求正文必须出现中小企业。relevant表示存在明确事件及产业或经营意义；"
        "irrelevant表示明确属于普通政务、民生、人事、网站内容且没有产业事件；"
        "无法确定时使用possibly_relevant。只返回JSON对象，字段必须为label、score、"
        "signal_types、industries、reason、evidence。evidence最多3条且必须逐字摘自输入正文。"
    )
