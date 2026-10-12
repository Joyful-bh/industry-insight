import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from pydantic import BaseModel, Field

from track_insight.core.errors import ModelCallError
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.infrastructure.bailian.contracts import ModelRunResult, WebSearchSource
from track_insight.settings import PocConfig, get_settings


class SearchQueries(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=4)


def parse_sources(responses: list[dict[str, Any]]) -> list[WebSearchSource]:
    sources: dict[str, WebSearchSource] = {}
    for response in responses:
        for item in (response.get("result") or {}).get("search_result") or []:
            link = str(item.get("link") or "").strip()
            if not link.startswith(("https://", "http://")):
                continue
            source = WebSearchSource(
                refer=f"ref_{len(sources) + 1}",
                title=item.get("title") or None,
                link=link,
                content=str(item.get("snippet") or item.get("content") or "")[:2000] or None,
                publish_date=(item.get("meta_info") or {}).get("publishedTime") or None,
            )
            previous = sources.get(link)
            if previous is None:
                sources[link] = source
            else:
                previous.title = previous.title or source.title
                previous.publish_date = previous.publish_date or source.publish_date
                if len(source.content or "") > len(previous.content or ""):
                    previous.content = source.content
    return list(sources.values())


def search_and_review(
    *,
    client: BailianClient,
    config: PocConfig,
    messages: list[dict[str, Any]],
    output_model: type[BaseModel],
    on_sources_discovered: Callable[[list[WebSearchSource]], None] | None = None,
) -> ModelRunResult:
    settings = get_settings()
    if not settings.opensearch_api_key or not settings.opensearch_base_url:
        raise ModelCallError(
            "OpenSearch requires OPENSEARCH_API_KEY and OPENSEARCH_BASE_URL",
            code="missing_opensearch_configuration",
        )
    task = json.loads(messages[-1]["content"])
    query_plan = client.complete_structured(
        messages=[
            {
                "role": "system",
                "content": (
                    "根据搜索任务生成直接交给搜索引擎的短查询。"
                    f"生成 {config.search.queries_per_task} 条互补查询，每条不超过35个汉字。"
                    "保留地区、制造业方向和具体信号意图，用政策/通知/申报或签约/开工/投产等"
                    "与任务相符的不同角度扩展。全国任务可以搜索全国或各省的相关材料。"
                    "政策任务的所有查询都围绕政策、通知、申报或扶持措施，不能改搜项目进度。"
                    "跨年份的任务应至少各有一条查询覆盖起始年和结束年。"
                    "年份应在任务时间范围内；不要粘贴起止日期区间，不用site语法，不指定域名，"
                    "不预设具体企业、项目或政策已经存在。只输出queries字符串列表。"
                ),
            },
            {"role": "user", "content": json.dumps(task, ensure_ascii=False)},
        ],
        output_model=SearchQueries,
        model=config.bailian.model,
        max_tokens=512,
        enable_thinking=False,
    )
    queries = list(dict.fromkeys(q.strip() for q in query_plan.output.queries if q.strip()))[
        : config.search.queries_per_task
    ]
    if not queries or any(len(q) > 80 or "site:" in q.lower() for q in queries):
        raise ModelCallError("Invalid search queries", code="invalid_search_queries")
    base_url = settings.opensearch_base_url.strip().rstrip("/")
    if base_url.startswith("http://"):
        base_url = "https://" + base_url[7:]
    if not base_url.startswith("https://"):
        raise ModelCallError(
            "OpenSearch requires an HTTPS endpoint", code="invalid_search_endpoint"
        )
    endpoint = (
        f"{base_url}/v3/openapi/workspaces/{settings.opensearch_workspace}"
        "/web-search/ops-web-search-001"
    )
    requests = [
        {
            "query": query,
            "query_rewrite": False,
            "top_k": config.search.top_k,
            "content_type": "snippet",
            "way": config.search.strategy,
        }
        for query in queries
    ]

    def search(http: httpx.Client, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = http.post(endpoint, json=payload)
        except httpx.RequestError as error:
            raise ModelCallError(str(error), retryable=True, code="search_network_error") from error
        if response.status_code >= 400:
            raise ModelCallError(
                f"OpenSearch HTTP {response.status_code}: {response.text[:1000]}",
                retryable=response.status_code == 429 or response.status_code >= 500,
                code=f"search_http_{response.status_code}",
            )
        try:
            result = response.json()
        except ValueError as error:
            raise ModelCallError(
                "OpenSearch returned invalid JSON", code="invalid_search_json"
            ) from error
        if not isinstance(result.get("result"), dict):
            raise ModelCallError(
                "OpenSearch returned no result object",
                code="invalid_search_response",
                diagnostics={"response": result},
            )
        return result

    with httpx.Client(
        headers={"Authorization": f"Bearer {settings.opensearch_api_key.get_secret_value()}"},
        timeout=config.search.timeout_seconds,
        transport=httpx.HTTPTransport(retries=1),
    ) as http:
        # OpenSearch's default limit is three requests per second.
        with ThreadPoolExecutor(max_workers=min(3, len(requests))) as pool:
            responses = list(pool.map(lambda payload: search(http, payload), requests))
    sources = parse_sources(responses)
    search_response = {
        "provider": "opensearch",
        "query_generation_response": query_plan.raw_response,
        "queries": queries,
        "search_calls": [
            {"request": request, "response": response}
            for request, response in zip(requests, responses, strict=True)
        ],
        "usage": {
            **query_plan.usage,
            "opensearch": {
                "calls": len(responses),
                "strategy": config.search.strategy,
                "estimated_search_cost_cny": round(
                    len(responses) * (0.012 if config.search.strategy == "lite" else 0.036), 6
                ),
            },
        },
    }
    if not sources:
        raise ModelCallError(
            "OpenSearch returned no usable source URLs",
            code="missing_search_sources",
            diagnostics={"search_response": search_response},
        )
    return client.review_search_sources(
        messages=messages,
        output_model=output_model,
        model=config.bailian.model,
        review_batch_size=config.bailian.review_batch_size,
        sources=sources,
        search_response=search_response,
        initial_call_count=query_plan.call_count + len(responses),
        initial_recovery_used=query_plan.recovery_used,
        on_sources_discovered=on_sources_discovered,
    )
