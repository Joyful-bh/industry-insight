import json
import urllib.error
import urllib.request
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from track_insight.enums import RelevanceLabel
from track_insight.relevance import RelevanceResult

PROMPT_VERSION = "relevance-semantic-v10-manufacturing"


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

    @field_validator("signal_types", "industries", mode="before")
    @classmethod
    def truncate_taxonomies(cls, value: object) -> object:
        return value[:12] if isinstance(value, list) else value

    @field_validator("evidence", mode="before")
    @classmethod
    def truncate_evidence(cls, value: object) -> object:
        return value[:3] if isinstance(value, list) else value


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
            "temperature": 0.6,
            "max_tokens": 800,
            "thinking": {"type": "disabled"},
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
            try:
                detail = error.read().decode("utf-8", errors="replace")[:500]
            except OSError:
                detail = ""
            if self.api_key:
                detail = detail.replace(self.api_key, "[redacted]")
            if error.code == 400 and "content_filter" in detail:
                return RelevanceResult(
                    label=RelevanceLabel.POSSIBLY_RELEVANT,
                    score=min(rule_result.score, 0.49),
                    signal_types=rule_result.signal_types,
                    industries=rule_result.industries,
                    matched_positive_rules=rule_result.matched_positive_rules,
                    matched_negative_rules=rule_result.matched_negative_rules,
                    reason="模型服务拒绝处理该正文，保留规则结果供人工复核。",
                    evidence=rule_result.evidence,
                )
            raise LlmClassificationError(
                f"LLM HTTP {error.code}: {detail or 'no response body'}", retryable=retryable
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
    valid_evidence = [evidence for evidence in output.evidence if evidence in source_text]
    if output.label != "possibly_relevant" and not valid_evidence:
        return output.model_copy(
            update={
                "label": "possibly_relevant",
                "score": min(output.score, 0.49),
                "reason": f"{output.reason[:450]}（证据未通过逐字校验，结果已降级。）",
                "evidence": [],
            }
        )
    return output.model_copy(update={"evidence": valid_evidence})


def _completion_endpoint(base_url: str) -> str:
    value = base_url.rstrip("/")
    return value if value.endswith("/chat/completions") else f"{value}/chat/completions"


def _system_prompt() -> str:
    return (
        "你负责筛选可用于发现北京制造业赛道变化的材料。只依据输入正文，不补充外部事实。"
        "关注制造业政策、申报认定、项目签约开工投产扩产、技术产业化、产业链供需、"
        "企业融资并购、产能订单价格等市场变化，以及明确适用于制造业中小企业的通用扶持政策。"
        "纯软件或互联网服务、金融产品宣传、文化旅游、居民生活、党建人事、会议过程、"
        "机构职能和无产业事实的活动报道判为irrelevant。"
        "relevant要求正文同时包含明确发生的事件，以及具体制造业对象、制造环节，或可执行的"
        "制造业中小企业支持条件。只有方向性表述、对象或事件不清时判为possibly_relevant。"
        "返回且仅返回一个JSON对象，字段必须完整且不得增加字段："
        '{"label":"relevant|possibly_relevant|irrelevant","score":0到1的小数,'
        '"signal_types":[],"industries":[],"reason":"简洁中文理由","evidence":[]}。'
        "signal_types使用英文蛇形命名；industries填写正文明确支持的具体制造业领域。"
        "relevant和irrelevant必须提供1至3条evidence；每条必须是输入text中连续、逐字一致的短摘录，"
        "不得改写，不得引用title或rule_assessment。possibly_relevant可不提供证据。"
    )
