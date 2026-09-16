import json
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from track_insight.core.errors import ModelCallError
from track_insight.infrastructure.bailian.contracts import ModelRunResult, WebSearchSource

OutputT = TypeVar("OutputT", bound=BaseModel)
logger = logging.getLogger(__name__)


class BailianClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: int = 120,
        max_retries: int = 2,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        transport = httpx.HTTPTransport(retries=max_retries)
        self.compatible_client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
        )

    def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        output_model: type[OutputT],
        model: str,
        max_tokens: int = 4096,
        enable_thinking: bool | None = None,
    ) -> ModelRunResult[OutputT]:
        schema_messages = _attach_schema(messages, output_model)
        payload = {
            "model": model,
            "messages": schema_messages,
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if enable_thinking is not None:
            payload["enable_thinking"] = enable_thinking
        first_response = self._post_compatible(
            "chat/completions", payload, operation="structured_completion"
        )
        try:
            return self._parse_response(first_response, output_model)
        except ModelCallError as first_error:
            repair_payload = {
                "model": model,
                "messages": [
                    *schema_messages,
                    {
                        "role": "user",
                        "content": (
                            "上次输出未通过结构校验。只修正格式，不增加解释。无论上次内容是否"
                            "完整，都必须返回满足JSON Schema的完整JSON对象。\n上次输出：\n"
                            + (_message_content(first_response) or "<empty>")
                        ),
                    },
                ],
                "temperature": 0,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            }
            if enable_thinking is not None:
                repair_payload["enable_thinking"] = enable_thinking
            repair_response = self._post_compatible(
                "chat/completions", repair_payload, operation="structured_repair"
            )
            try:
                result = self._parse_response(repair_response, output_model)
            except ModelCallError as repair_error:
                raise _combined_validation_error(
                    first_error, first_response, repair_error, repair_response
                ) from repair_error
            result.raw_response = {
                "initial_response": first_response,
                "repair_response": repair_response,
            }
            result.call_count = 2
            result.recovery_used = True
            return result

    def search_and_review(
        self,
        *,
        messages: list[dict[str, Any]],
        output_model: type[OutputT],
        model: str,
        review_batch_size: int,
        on_sources_discovered: Callable[[list[WebSearchSource]], None] | None = None,
    ) -> ModelRunResult[OutputT]:
        task_content = _last_user_content(messages)
        query = _extract_search_query(task_content)
        search_payload = {
            "model": model,
            "instructions": (
                "必须使用联网搜索完成查询。优先寻找原始政策、机构、园区、企业和权威产业"
                "来源；回答中简要概括搜索结果，勿编造网址。"
            ),
            "input": query,
            "tools": [{"type": "web_search"}],
            "tool_choice": "required",
            "enable_thinking": False,
            "max_output_tokens": 1200,
            "store": False,
        }
        search_response = self._post_compatible(
            "responses",
            search_payload,
            operation="responses_web_search",
        )
        sources = _parse_search_sources(search_response)
        if not sources:
            raise ModelCallError(
                "Bailian response contains no search sources with URLs",
                code="missing_search_sources",
                diagnostics={"initial_response": search_response},
            )
        if on_sources_discovered is not None:
            on_sources_discovered(sources)
        batches = [
            sources[index : index + review_batch_size]
            for index in range(0, len(sources), review_batch_size)
        ]
        judgments: list[dict[str, Any]] = []
        review_responses: list[dict[str, Any]] = []
        usage: dict[str, Any] = dict(search_response.get("usage") or {})
        call_count = 1
        recovery_used = False
        last_request_id: str | None = None
        for batch_index, batch in enumerate(batches, start=1):
            logger.info(
                "Search review batch started",
                extra={
                    "event_type": "search.review_batch.started",
                    "batch_index": batch_index,
                    "batch_count": len(batches),
                    "source_count": len(batch),
                },
            )
            review_messages = _review_messages(
                messages=messages,
                task_content=task_content,
                search_response=search_response,
                sources=batch,
                batch_index=batch_index,
                batch_count=len(batches),
            )
            try:
                review = self.complete_structured(
                    messages=review_messages,
                    output_model=output_model,
                    model=model,
                    enable_thinking=False,
                )
                _validate_reference_coverage(review.output, batch)
            except ModelCallError as error:
                error.diagnostics.setdefault("search_response", search_response)
                error.diagnostics.setdefault("review_batch_index", batch_index)
                error.diagnostics.setdefault("review_batch_count", len(batches))
                raise
            batch_judgments = getattr(review.output, "judgments", [])
            judgments.extend(item.model_dump(mode="json") for item in batch_judgments)
            review_responses.append(review.raw_response)
            usage = _merge_usage(usage, review.usage)
            call_count += review.call_count
            recovery_used = recovery_used or review.recovery_used
            last_request_id = review.request_id
            logger.info(
                "Search review batch completed",
                extra={
                    "event_type": "search.review_batch.completed",
                    "batch_index": batch_index,
                    "batch_count": len(batches),
                    "source_count": len(batch),
                    "request_id": review.request_id,
                    "recovery_used": review.recovery_used,
                },
            )
        output = output_model.model_validate({"judgments": judgments})
        _validate_reference_coverage(output, sources)
        return ModelRunResult[output_model](
            request_id=last_request_id or search_response.get("id"),
            model=model,
            usage=usage,
            raw_response={
                "search_response": search_response,
                "review_responses": review_responses,
            },
            output=output,
            web_search=sources,
            call_count=call_count,
            recovery_used=recovery_used,
        )

    def _post_compatible(
        self, endpoint: str, payload: dict[str, Any], *, operation: str
    ) -> dict[str, Any]:
        return self._post(self.compatible_client, endpoint, payload, operation=operation)

    @staticmethod
    def _post(
        client: httpx.Client,
        endpoint: str,
        payload: dict[str, Any],
        *,
        operation: str,
    ) -> dict[str, Any]:
        started = time.monotonic()
        logger.info(
            "Bailian request started",
            extra={
                "event_type": "model.request.started",
                "provider": "bailian",
                "operation": operation,
                "model": payload.get("model"),
            },
        )
        try:
            response = client.post(endpoint, json=payload)
        except httpx.RequestError as error:
            duration_ms = int((time.monotonic() - started) * 1000)
            raise ModelCallError(
                str(error),
                retryable=True,
                code="network_error",
                diagnostics={"operation": operation, "duration_ms": duration_ms},
            ) from error
        duration_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            detail = response.text[:2000]
            retryable = response.status_code == 429 or response.status_code >= 500
            logger.error(
                "Bailian request returned an error",
                extra={
                    "event_type": "model.request.http_error",
                    "provider": "bailian",
                    "operation": operation,
                    "duration_ms": duration_ms,
                    "http_status": response.status_code,
                },
            )
            raise ModelCallError(
                f"Bailian HTTP {response.status_code}: {detail}",
                retryable=retryable,
                code=f"http_{response.status_code}",
                diagnostics={
                    "operation": operation,
                    "duration_ms": duration_ms,
                    "http_status": response.status_code,
                    "response_excerpt": detail,
                },
            )
        try:
            result = response.json()
        except ValueError as error:
            raise ModelCallError(
                "Bailian returned invalid JSON",
                code="invalid_api_json",
                diagnostics={"operation": operation, "response_excerpt": response.text[:2000]},
            ) from error
        if result.get("error"):
            error_payload = result["error"]
            code = str(error_payload.get("code") or "unknown")
            message = str(error_payload.get("message") or "unknown Bailian API error")
            retryable = code in {"Throttling", "InternalError", "ServiceUnavailable"}
            raise ModelCallError(
                f"Bailian API error {code}: {message}",
                retryable=retryable,
                code=f"api_{code}",
                diagnostics={"operation": operation, "response": result},
            )
        logger.info(
            "Bailian request completed",
            extra={
                "event_type": "model.request.completed",
                "provider": "bailian",
                "operation": operation,
                "duration_ms": duration_ms,
                "http_status": response.status_code,
                "request_id": result.get("request_id") or result.get("id"),
                "usage": result.get("usage") or {},
                "raw_search_result_count": len(_parse_search_sources(result)),
            },
        )
        return result

    @staticmethod
    def _parse_response(
        payload: dict[str, Any], output_model: type[OutputT]
    ) -> ModelRunResult[OutputT]:
        try:
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("message content is not text")
            cleaned = content.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.removeprefix("```json").removeprefix("```")
                cleaned = cleaned.removesuffix("```").strip()
            output = output_model.model_validate_json(cleaned)
        except (KeyError, IndexError, TypeError, ValidationError, ValueError) as error:
            raise ModelCallError(
                f"structured response validation failed: {error}",
                code="invalid_model_output",
                diagnostics={
                    "request_id": payload.get("request_id") or payload.get("id"),
                    "usage": payload.get("usage") or {},
                    "validation_error": str(error),
                    "response": payload,
                },
            ) from error
        return ModelRunResult[output_model](
            request_id=payload.get("request_id") or payload.get("id"),
            model=str(payload.get("model", "")),
            usage=payload.get("usage") or {},
            raw_response=payload,
            output=output,
        )


def _parse_search_sources(payload: dict[str, Any]) -> list[WebSearchSource]:
    sources: list[WebSearchSource] = []
    seen: set[str] = set()
    for output_item in payload.get("output") or []:
        if not isinstance(output_item, dict) or output_item.get("type") != "web_search_call":
            continue
        action = output_item.get("action") or {}
        for source in action.get("sources") or []:
            if not isinstance(source, dict) or not source.get("url"):
                continue
            link = str(source["url"])
            if link in seen:
                continue
            seen.add(link)
            sources.append(
                WebSearchSource(
                    refer=f"ref_{len(sources) + 1}",
                    title=source.get("title"),
                    link=link,
                    content=source.get("snippet"),
                    media=source.get("site_name"),
                    publish_date=source.get("publish_time"),
                )
            )
    return sources


def _review_messages(
    *,
    messages: list[dict[str, Any]],
    task_content: str,
    search_response: dict[str, Any],
    sources: list[WebSearchSource],
    batch_index: int,
    batch_count: int,
) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": (
                _system_content(messages)
                + "\n\n你只审阅当前批次给定的结果，不再联网搜索。"
                "每条输入恰好输出一条判断，并逐字复制 refer；"
                "不得遗漏、合并、增加或改写 refer。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": task_content,
                    "batch": {"index": batch_index, "count": batch_count},
                    "search_answer": _response_output_text(search_response),
                    "required_refers": [source.refer for source in sources],
                    "search_results": [source.model_dump(mode="json") for source in sources],
                    "required_output": {
                        "judgments": [
                            {
                                "refer": "逐字复制输入 refer",
                                "decision": "keep|maybe|drop",
                                "url_type": "content_page|source_entry",
                                "source_class": (
                                    "government|park|association|company|industry_media|"
                                    "authoritative_media|investment_institution|other"
                                ),
                                "reason": "简短中文判断依据",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]


def _merge_usage(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_usage(current, value)
        elif isinstance(current, int) and isinstance(value, int):
            merged[key] = current + value
        elif current is None:
            merged[key] = value
    return merged


def _attach_schema(
    messages: list[dict[str, Any]], output_model: type[BaseModel]
) -> list[dict[str, Any]]:
    instruction = "\n\n只输出JSON，且必须严格满足以下JSON Schema：\n" + json.dumps(
        output_model.model_json_schema(), ensure_ascii=False
    )
    result = [dict(message) for message in messages]
    for message in result:
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            message["content"] += instruction
            break
    else:
        result.insert(0, {"role": "system", "content": instruction.strip()})
    return result


def _last_user_content(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    raise ModelCallError("search call has no user message", code="invalid_search_input")


def _extract_search_query(content: str) -> str:
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return content
    if isinstance(payload, dict):
        query = payload.get("query")
        if isinstance(query, str) and query.strip():
            return query.strip()
    return content


def _response_output_text(payload: dict[str, Any]) -> str | None:
    texts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    texts.append(text)
    return "\n".join(texts) or None


def _system_content(messages: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        message["content"]
        for message in messages
        if message.get("role") == "system" and isinstance(message.get("content"), str)
    )


def _message_content(payload: dict[str, Any]) -> str | None:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return content if isinstance(content, str) else None


def _validate_reference_coverage(output: BaseModel, sources: list[WebSearchSource]) -> None:
    judgments = getattr(output, "judgments", None)
    if judgments is None:
        return
    actual = [item.refer for item in judgments]
    expected = [item.refer for item in sources]
    if len(actual) != len(expected) or set(actual) != set(expected):
        raise ModelCallError(
            "search judgments must cover every returned refer exactly once",
            code="search_reference_mismatch",
            diagnostics={"expected_refers": expected, "actual_refers": actual},
        )


def _combined_validation_error(
    first_error: ModelCallError,
    first_response: dict[str, Any],
    recovery_error: ModelCallError,
    recovery_response: dict[str, Any],
) -> ModelCallError:
    return ModelCallError(
        f"initial and recovery outputs both failed validation: {recovery_error}",
        code="invalid_model_output_after_recovery",
        diagnostics={
            "initial_error": str(first_error),
            "recovery_error": str(recovery_error),
            "initial_response": first_response,
            "recovery_response": recovery_response,
        },
    )
