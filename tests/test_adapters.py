import json
from datetime import UTC, datetime
from types import SimpleNamespace

from track_insight.acquisition import FetchResponse
from track_insight.adapters import (
    BeijingSmeServiceAdapter,
    HicoolProjectsApiAdapter,
    PaginatedOfficialAdapter,
    _canonicalize_dated_context_url,
    _discovery_filter_decision,
    _same_site,
    default_cutoff,
    months_ago,
)
from track_insight.scope_config import CollectionScope


def test_months_ago_uses_calendar_months() -> None:
    reference = datetime(2026, 3, 31, 8, 30, tzinfo=UTC)

    assert months_ago(reference, 1) == datetime(2026, 2, 28, 8, 30, tzinfo=UTC)
    assert months_ago(reference, 24) == datetime(2024, 3, 31, 8, 30, tzinfo=UTC)


def test_index_pagination_url() -> None:
    assert (
        PaginatedOfficialAdapter._page_url("https://www.stats.gov.cn/sj/zxfb/index.html", 2)
        == "https://www.stats.gov.cn/sj/zxfb/index_2.html"
    )


def test_government_policy_home_pagination_url() -> None:
    assert PaginatedOfficialAdapter._page_url(
        "https://www.gov.cn/zhengce/zhengceku/bmwj/home.htm", 2
    ) == "https://www.gov.cn/zhengce/zhengceku/bmwj/home_2.htm"


def test_government_cms_fragment_is_a_pagination_link() -> None:
    from track_insight.adapters import _is_pagination_link, _page_number, _select_next_page

    url = "https://example.gov.cn/zcwj/f3035c13-12.html"
    assert _is_pagination_link(url, "12")
    assert _page_number(url) == 12
    assert _select_next_page(
        "https://example.gov.cn/zcwj/f3035c13-2.html",
        [
            "https://example.gov.cn/zcwj/f3035c13-1.html",
            "https://example.gov.cn/zcwj/f3035c13-3.html",
        ],
        set(),
        [],
    ) == "https://example.gov.cn/zcwj/f3035c13-3.html"


def test_default_cutoff_includes_the_whole_boundary_day() -> None:
    scope = CollectionScope.model_validate(
        {
            "version": "test",
            "target_regions": [{"code": "CN-11", "name": "北京市"}],
            "backfill_months": 24,
            "document_formats": ["html", "pdf"],
        }
    )

    assert default_cutoff(scope, datetime(2026, 9, 11, 15, 10, tzinfo=UTC)) == datetime(
        2024, 9, 11, tzinfo=UTC
    )


def test_same_government_site_accepts_channel_subdomains() -> None:
    assert _same_site("https://www.bjhd.gov.cn/", "https://zyk.bjhd.gov.cn/zwdt/")
    assert not _same_site("https://www.bjhd.gov.cn/", "https://www.beijing.gov.cn/")


def test_same_site_handles_chinese_com_cn_domains() -> None:
    assert _same_site("https://www.bjd.com.cn/", "https://news.bjd.com.cn/article")
    assert not _same_site("https://www.bjd.com.cn/", "https://www.example.com.cn/")


def test_dated_context_url_preserves_cepea_bare_query_path() -> None:
    assert _canonicalize_dated_context_url("https://cepea.com/?list_26/728.html") == (
        "https://cepea.com/?list_26/728.html"
    )


def test_discovery_filter_keeps_business_signal_and_rejects_life_service() -> None:
    config = {
        "enabled": True,
        "exploration_rate": 0,
        "include_any": ["专精特新"],
        "include_all_groups": [["申报", "认定"], ["企业", "产业"]],
        "exclude_any": ["入学", "养老"],
    }

    assert _discovery_filter_decision(
        config,
        url="https://example.gov.cn/2026/1.html",
        title="专精特新中小企业申报通知",
        context="",
    ) == "matched"
    assert _discovery_filter_decision(
        config,
        url="https://example.gov.cn/2026/2.html",
        title="义务教育入学通知",
        context="",
    ) == "filtered"


def test_discovery_filter_exploration_is_deterministic() -> None:
    config = {"enabled": True, "exploration_rate": 1}
    args = {
        "url": "https://example.gov.cn/2026/unknown.html",
        "title": "未命中标题",
        "context": "",
    }

    assert _discovery_filter_decision(config, **args) == "exploration"
    assert _discovery_filter_decision(config, **args) == "exploration"


def test_discovery_filter_url_allowlist_is_a_hard_boundary() -> None:
    config = {
        "enabled": True,
        "exploration_rate": 1,
        "allow_url_prefixes": ["/zwgk/zcjjd/zcwj/", "/zwgk/zcjjd/zcjd/"],
    }

    assert _discovery_filter_decision(
        config,
        url="https://zdb.beijing.gov.cn/zwgk/zcjjd/zcwj/2026/policy.html",
        title="政策文件",
        context="",
    ) == "matched"
    assert _discovery_filter_decision(
        config,
        url="https://zdb.beijing.gov.cn/zwgk/ldjs/2026/leader.html",
        title="领导介绍",
        context="",
    ) == "filtered"


def test_hicool_api_adapter_materializes_project_as_html() -> None:
    payload = {
        "code": 0,
        "data": [
            {
                "id": "787",
                "title": "青龙人形机器人",
                "tag": "机器人,HICOOL",
                "city": "北京",
                "introduce": "项目介绍正文",
                "create_time": "2026-08-23 14:03:00",
            }
        ],
    }

    class Fetcher:
        def fetch(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResponse:
            return FetchResponse(
                url=url,
                status_code=200,
                headers={"content-type": "application/json"},
                content=json.dumps(payload).encode(),
            )

    source = SimpleNamespace(entry_urls=["https://hicool.com/matchmaking/"])
    batch = HicoolProjectsApiAdapter(max_pages=1, max_items=3).discover(source, Fetcher())

    assert len(batch.items) == 1
    assert batch.items[0].external_id == "787"
    assert batch.items[0].published_at == datetime(2026, 8, 23, 14, 3, tzinfo=UTC)
    assert "项目介绍正文".encode() in batch.items[0].inline_content


def test_sme_adapter_reads_vue_bound_detail_links() -> None:
    class Fetcher:
        def fetch(self, url: str) -> FetchResponse:
            return FetchResponse(
                url=url,
                status_code=200,
                headers={"content-type": "text/html"},
                content=(
                    b'<li><a :href="GLOBAL ? \'javascript:void(0)\' : '
                    b"'../detail/6aa36f639cd51417f26601e2.html'\"><h3>activity</h3>"
                    b"<span>2026-09-09</span></a></li>"
                ),
            )

    source = SimpleNamespace(entry_urls=["https://www.smebj.cn/activity/activity1.html"])
    batch = BeijingSmeServiceAdapter(max_pages=1, max_items=10).discover(source, Fetcher())

    assert len(batch.items) == 1
    assert batch.items[0].metadata["source_section"] == "activity"
