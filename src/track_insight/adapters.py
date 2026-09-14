import calendar
import hashlib
import html
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from track_insight.acquisition import (
    AccessBlockedError,
    CollectionError,
    DiscoveredItem,
    DiscoveryBatch,
    HttpFetcher,
    canonicalize_url,
    extract_html_link_records,
    published_at_from_url,
)
from track_insight.models import Source
from track_insight.scope_config import CollectionScope

ARTICLE_DATE_PATTERN = re.compile(
    r"(?:/|_)(20\d{2})(?:[-/]?)(0?[1-9]|1[0-2])(?:[-/]?)(0?[1-9]|[12]\d|3[01])(?:/|_|\.)"
)
SLUG_ARTICLE_PATTERN = re.compile(r"/plat/(?:news|cttl-t)/[a-z0-9][a-z0-9\-‑]{19,}$", re.I)
PAGINATION_PATTERN = re.compile(
    r"(?:index|page|list)[_-]?(\d+)\.(?:s?html?)$"
    r"|[?&](?:page|pageNo|pageNum)=(\d+)"
    r"|/[a-f0-9][a-f0-9-]{7,}-(\d+)\.html$",
    re.IGNORECASE,
)
NEXT_PAGE_TITLES = {"下一页", "下页", "后一页", "next", ">", "›", "»"}
NON_ARTICLE_TITLES = {
    "首页",
    "上一页",
    "下一页",
    "末页",
    "更多",
    "登录",
    "注册",
    "网站地图",
    "English",
}


def months_ago(reference: datetime, months: int) -> datetime:
    month_index = reference.year * 12 + reference.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(reference.day, calendar.monthrange(year, month)[1])
    return reference.replace(year=year, month=month, day=day)


class PaginatedOfficialAdapter:
    """用于按 index_N.html 顺序翻页的政府列表页。"""

    def __init__(
        self,
        *,
        article_pattern: str,
        max_pages: int = 5,
        max_items: int = 5000,
    ) -> None:
        self.article_pattern = re.compile(article_pattern)
        self.max_pages = max_pages
        self.max_items = max_items

    def discover(
        self,
        source: Source,
        fetcher: HttpFetcher,
        *,
        cutoff: datetime | None = None,
        cursor: dict[str, Any] | None = None,
        known_urls: set[str] | None = None,
        stop_after_known: int = 0,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> DiscoveryBatch:
        items: list[DiscoveredItem] = []
        reports: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        seen: set[str] = set()
        known = known_urls or set()
        entry_index = int((cursor or {}).get("entry_index", 0))
        page_number = int((cursor or {}).get("page_number", 0))
        pages_visited = 0
        known_streak = 0

        while entry_index < len(source.entry_urls) and pages_visited < self.max_pages:
            entry_url = source.entry_urls[entry_index]
            page_url = self._page_url(entry_url, page_number)
            try:
                response = fetcher.fetch(page_url)
            except AccessBlockedError:
                raise
            except CollectionError as error:
                failure = {"url": page_url, "error": str(error)}
                failures.append(failure)
                report = {"url": page_url, "status": "failed", "error": str(error)}
                reports.append(report)
                entry_index += 1
                page_number = 0
                pages_visited += 1
                _emit(progress, report)
                continue
            if response.mime_type != "text/html":
                failure = {"url": page_url, "error": "list page is not HTML"}
                failures.append(failure)
                reports.append({"url": page_url, "status": "failed", **failure})
                entry_index += 1
                page_number = 0
                pages_visited += 1
                continue

            discovered_on_page = 0
            dated_on_page = 0
            old_on_page = 0
            stop_for_known = False
            for link in extract_html_link_records(response):
                href, title = link.href, link.title
                try:
                    url = canonicalize_url(urljoin(response.url, href))
                except CollectionError:
                    continue
                if url in seen or not self.article_pattern.search(urlsplit(url).path):
                    continue
                seen.add(url)
                published_at = _published_at_from_url_and_text(url, link.context)
                if published_at is not None:
                    dated_on_page += 1
                    if cutoff is not None and published_at < cutoff:
                        old_on_page += 1
                        continue
                known_streak = known_streak + 1 if url in known else 0
                items.append(
                    DiscoveredItem(
                        url=url,
                        title=title or None,
                        published_at=published_at,
                        metadata={"list_page": page_url},
                    )
                )
                discovered_on_page += 1
                if stop_after_known and known_streak >= stop_after_known:
                    stop_for_known = True
                    break
                if len(items) >= self.max_items:
                    break

            pages_visited += 1
            report = {
                "url": page_url,
                "status": "ok",
                "items": discovered_on_page,
                "dated_items": dated_on_page,
                "old_items": old_on_page,
            }
            reports.append(report)
            _emit(progress, report)
            page_number += 1

            reached_cutoff = bool(dated_on_page and old_on_page)
            if reached_cutoff or stop_for_known:
                return DiscoveryBatch(
                    items,
                    {
                        "entry_index": entry_index,
                        "page_number": page_number,
                        "complete": True,
                        "reason": "cutoff" if reached_cutoff else "known_streak",
                    },
                    reports,
                    failures,
                )
            if len(items) >= self.max_items:
                break

        complete = entry_index >= len(source.entry_urls)
        return DiscoveryBatch(
            items,
            {
                "entry_index": entry_index,
                "page_number": page_number,
                "complete": complete,
                "reason": "entries_complete" if complete else "page_batch_complete",
            },
            reports,
            failures,
        )

    @staticmethod
    def _page_url(entry_url: str, page_number: int) -> str:
        if page_number == 0:
            return entry_url
        parts = urlsplit(entry_url)
        path = parts.path
        if path.endswith(("home.htm", "home.html")):
            stem, extension = path.rsplit(".", 1)
            path = f"{stem}_{page_number}.{extension}"
        elif path.endswith("index.html"):
            path = f"{path[: -len('index.html')]}index_{page_number}.html"
        elif path.endswith("/"):
            path = f"{path}index_{page_number}.html"
        else:
            raise CollectionError(f"unsupported pagination entry URL: {entry_url}")
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


class GenericSiteAdapter:
    """高召回、站内受限的通用列表发现器；不做SMB语义过滤。"""

    def __init__(self, *, max_pages: int = 5, max_items: int = 5000) -> None:
        self.max_pages = max_pages
        self.max_items = max_items

    def discover(
        self,
        source: Source,
        fetcher: HttpFetcher,
        *,
        cutoff: datetime | None = None,
        cursor: dict[str, Any] | None = None,
        known_urls: set[str] | None = None,
        stop_after_known: int = 0,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> DiscoveryBatch:
        pending = list((cursor or {}).get("pending_urls") or source.entry_urls)
        items: list[DiscoveredItem] = []
        reports: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        visited: set[str] = set()
        seen_items: set[str] = set()
        known = known_urls or set()
        known_streak = 0
        stop_reason: str | None = None
        completion_reason: str | None = None

        while pending and len(reports) < self.max_pages and len(items) < self.max_items:
            page_url = pending.pop(0)
            if page_url in visited:
                continue
            visited.add(page_url)
            try:
                response = fetcher.fetch(page_url)
            except AccessBlockedError:
                raise
            except CollectionError as error:
                failure = {"url": page_url, "error": str(error)}
                failures.append(failure)
                report = {"url": page_url, "status": "failed", "error": str(error)}
                reports.append(report)
                _emit(progress, report)
                continue
            if response.mime_type != "text/html":
                failure = {"url": page_url, "error": "list page is not HTML"}
                failures.append(failure)
                reports.append({"url": page_url, "status": "failed", **failure})
                continue

            page_items: list[DiscoveredItem] = []
            page_links: list[str] = []
            old_items = 0
            filtered_items = 0
            exploration_items = 0
            for link in extract_html_link_records(response):
                href, title = link.href, link.title
                try:
                    url = canonicalize_url(urljoin(response.url, href))
                except CollectionError:
                    continue
                if not _same_site(response.url, url):
                    continue
                if _is_pagination_link(url, title):
                    page_links.append(url)
                published_at = _published_at_from_url_and_text(url, link.context)
                if url in seen_items or not _is_article_link(
                    url,
                    title,
                    published_at,
                    _normalized_terms(source.discovery_filter.get("article_url_patterns")),
                ):
                    continue
                seen_items.add(url)
                if cutoff is not None and published_at is not None and published_at < cutoff:
                    old_items += 1
                    continue
                filter_decision = _discovery_filter_decision(
                    source.discovery_filter, url=url, title=title, context=link.context
                )
                if filter_decision == "filtered":
                    filtered_items += 1
                    continue
                if filter_decision == "exploration":
                    exploration_items += 1
                known_streak = known_streak + 1 if url in known else 0
                page_items.append(
                    DiscoveredItem(
                        url=url,
                        title=title or None,
                        published_at=published_at,
                        metadata={
                            "list_page": page_url,
                            "discovery_filter": filter_decision,
                        },
                    )
                )
                if stop_after_known and known_streak >= stop_after_known:
                    stop_reason = "known_streak"
                    break
            items.extend(page_items[: max(0, self.max_items - len(items))])
            reached_cutoff = bool(old_items and not page_items)
            next_page = _select_next_page(page_url, page_links, visited, pending)
            if next_page and not reached_cutoff and not stop_reason:
                pending.insert(0, next_page)
            report = {
                "url": page_url,
                "status": "ok",
                "items": len(page_items),
                "old_items": old_items,
                "filtered_items": filtered_items,
                "exploration_items": exploration_items,
                "next_page": next_page,
            }
            reports.append(report)
            _emit(progress, report)
            if stop_reason or reached_cutoff:
                if stop_reason:
                    pending.clear()
                else:
                    completion_reason = "cutoff"
                known_streak = 0

        complete = not pending
        if not items and reports and not failures:
            failures.append(
                {
                    "url": reports[0]["url"],
                    "error": "no documents discovered; entry or adapter structure requires review",
                }
            )
        return DiscoveryBatch(
            items,
            {
                "pending_urls": pending,
                "complete": complete,
                "reason": stop_reason
                or completion_reason
                or ("entries_complete" if complete else "page_batch_complete"),
            },
            reports,
            failures,
        )


def _discovery_filter_decision(
    config: dict[str, Any] | None, *, url: str, title: str, context: str
) -> str:
    """Return matched, exploration, filtered, or disabled for a list candidate."""
    if not config or not config.get("enabled", False):
        return "disabled"

    path = urlsplit(url).path.lower()
    allowed_prefixes = _normalized_terms(config.get("allow_url_prefixes"))
    denied_prefixes = _normalized_terms(config.get("deny_url_prefixes"))
    if allowed_prefixes and not any(path.startswith(prefix) for prefix in allowed_prefixes):
        return "filtered"
    if denied_prefixes and any(path.startswith(prefix) for prefix in denied_prefixes):
        return "filtered"

    haystack = " ".join((title or "", context or "")).lower()
    include_any = _normalized_terms(config.get("include_any"))
    include_groups = [
        _normalized_terms(group) for group in config.get("include_all_groups", []) if group
    ]
    exclude_any = _normalized_terms(config.get("exclude_any"))

    if allowed_prefixes and not include_any and not include_groups and not exclude_any:
        return "matched"

    strong_match = any(term in haystack for term in include_any)
    grouped_match = bool(include_groups) and all(
        any(term in haystack for term in group) for group in include_groups
    )
    if strong_match or grouped_match:
        return "matched"

    # Exclusions are not absolute: the deterministic exploration sample still
    # keeps a small, repeatable window into unmatched sections.
    rate = min(max(float(config.get("exploration_rate", 0.0)), 0.0), 1.0)
    bucket = int(hashlib.sha256(url.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    if bucket < rate:
        return "exploration"
    if any(term in haystack for term in exclude_any):
        return "filtered"
    return "filtered"


def _normalized_terms(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(term).strip().lower() for term in value if str(term).strip()]


class StatsNationalAdapter(PaginatedOfficialAdapter):
    def __init__(self, *, max_pages: int = 5, max_items: int = 5000) -> None:
        super().__init__(
            article_pattern=r"/sj/zxfb/20\d{4}/t20\d{6}_\d+\.html$",
            max_pages=max_pages,
            max_items=max_items,
        )


class GovPolicyLibraryAdapter(PaginatedOfficialAdapter):
    def __init__(self, *, max_pages: int = 5, max_items: int = 5000) -> None:
        super().__init__(
            article_pattern=r"/zhengce/zhengceku/20\d{4}/content_\d+\.htm$",
            max_pages=max_pages,
            max_items=max_items,
        )


class BeijingPolicyAdapter(PaginatedOfficialAdapter):
    def __init__(self, *, max_pages: int = 5, max_items: int = 5000) -> None:
        super().__init__(
            article_pattern=r"/zhengce/zhengcefagui/20\d{4}/t20\d{6}_\d+\.html$",
            max_pages=max_pages,
            max_items=max_items,
        )


class FangshanPolicyAdapter:
    """Read the JSON payload embedded by Fangshan's policy-library shell page."""

    def __init__(self, *, max_pages: int = 5, max_items: int = 5000) -> None:
        del max_pages
        self.max_items = max_items

    def discover(self, source: Source, fetcher: HttpFetcher, **kwargs: Any) -> DiscoveryBatch:
        cutoff = kwargs.get("cutoff")
        progress = kwargs.get("progress")
        page_url = source.entry_urls[0]
        response = fetcher.fetch(page_url)
        soup = BeautifulSoup(response.content, "html.parser")
        payload = soup.select_one("#json")
        if payload is None:
            failure = {"url": page_url, "error": "embedded policy JSON was not found"}
            return DiscoveryBatch([], {"complete": False}, [], [failure])
        records = json.loads(payload.get_text(strip=True))
        items: list[DiscoveredItem] = []
        old_items = 0
        for record in records:
            if not record.get("url"):
                continue
            published_at = _parse_datetime(record.get("time"))
            if cutoff and published_at and published_at < cutoff:
                old_items += 1
                continue
            items.append(
                DiscoveredItem(
                    url=canonicalize_url(record["url"]),
                    title=record.get("title"),
                    published_at=published_at,
                    metadata={"list_page": page_url},
                )
            )
            if len(items) >= self.max_items:
                break
        report = {"url": page_url, "status": "ok", "items": len(items), "old_items": old_items}
        _emit(progress, report)
        return DiscoveryBatch(
            items, {"complete": True, "reason": "embedded_list_complete"}, [report]
        )


class HaidianPolicyApiAdapter:
    """Use the public JSON request made by Haidian's policy-library page."""

    def __init__(self, *, max_pages: int = 5, max_items: int = 5000) -> None:
        self.max_pages = max_pages
        self.max_items = max_items

    def discover(self, source: Source, fetcher: HttpFetcher, **kwargs: Any) -> DiscoveryBatch:
        cutoff = kwargs.get("cutoff")
        progress = kwargs.get("progress")
        page = int((kwargs.get("cursor") or {}).get("page", 1))
        items: list[DiscoveredItem] = []
        reports: list[dict[str, Any]] = []
        reason = "page_batch_complete"
        for _ in range(self.max_pages):
            page_url = _replace_query(source.entry_urls[0], pageNum=page, pageSize=20)
            response = fetcher.fetch(
                page_url,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://zyk.bjhd.gov.cn/zwdt/zcml/?fl=2&zt=1",
                },
            )
            payload = json.loads(response.content)
            rows = payload.get("rows", [])
            old_items = 0
            for record in rows:
                published_at = _parse_datetime(record.get("pubDate"))
                if cutoff and published_at and published_at < cutoff:
                    old_items += 1
                    continue
                items.append(DiscoveredItem(
                    url=canonicalize_url(record["docPubUrl"]), title=record.get("title"),
                    published_at=published_at, metadata={"list_page": page_url},
                ))
            report = {
                "url": page_url,
                "status": "ok",
                "items": len(rows) - old_items,
                "old_items": old_items,
            }
            reports.append(report)
            _emit(progress, report)
            page += 1
            if not rows or old_items:
                reason = "entries_complete" if not rows else "cutoff"
                break
            if len(items) >= self.max_items:
                break
        complete = reason != "page_batch_complete"
        return DiscoveryBatch(
            items[: self.max_items],
            {"page": page, "complete": complete, "reason": reason},
            reports,
        )


class StandardsSamrAdapter:
    """Extract standards from SAMR's server-rendered result rows and JS detail IDs."""

    def __init__(self, *, max_pages: int = 5, max_items: int = 5000) -> None:
        self.max_pages = max_pages
        self.max_items = max_items

    def discover(self, source: Source, fetcher: HttpFetcher, **kwargs: Any) -> DiscoveryBatch:
        cutoff = kwargs.get("cutoff")
        progress = kwargs.get("progress")
        page = int((kwargs.get("cursor") or {}).get("page", 1))
        items: list[DiscoveredItem] = []
        reports: list[dict[str, Any]] = []
        reason = "page_batch_complete"
        for _ in range(self.max_pages):
            page_url = _replace_query(source.entry_urls[0], page=page, pageSize=10)
            response = fetcher.fetch(page_url)
            soup = BeautifulSoup(response.content, "html.parser")
            rows = soup.select("tr")
            found = 0
            old_items = 0
            for row in rows:
                trigger = row.select_one('[onclick*="showInfo"]')
                match = (
                    re.search(
                        r"showInfo\(['\"]([A-Fa-f0-9]+)", trigger.get("onclick", "")
                    )
                    if trigger
                    else None
                )
                cells = [
                    " ".join(cell.get_text(" ", strip=True).split())
                    for cell in row.select("td")
                ]
                if not match or len(cells) < 7:
                    continue
                published_at = _parse_datetime(cells[-3])
                if cutoff and published_at and published_at < cutoff:
                    old_items += 1
                    continue
                detail_url = urljoin(response.url, f"newGbInfo?hcno={match.group(1)}")
                items.append(DiscoveredItem(
                    url=canonicalize_url(detail_url), title=f"{cells[1]} {cells[2]}",
                    published_at=published_at, external_id=match.group(1),
                    metadata={"list_page": page_url, "standard_no": cells[1]},
                ))
                found += 1
            report = {"url": page_url, "status": "ok", "items": found, "old_items": old_items}
            reports.append(report)
            _emit(progress, report)
            page += 1
            if found == 0 or old_items:
                reason = "entries_complete" if found == 0 else "cutoff"
                break
            if len(items) >= self.max_items:
                break
        complete = reason != "page_batch_complete"
        return DiscoveryBatch(
            items[: self.max_items],
            {"page": page, "complete": complete, "reason": reason},
            reports,
        )


class BseDisclosureAdapter:
    """Collect public BSE announcement metadata through its official list request."""

    endpoint = "https://www.bse.cn/disclosureInfoController/initDisclosureList.do"

    def __init__(self, *, max_pages: int = 5, max_items: int = 5000) -> None:
        self.max_pages = max_pages
        self.max_items = max_items

    def discover(self, source: Source, fetcher: HttpFetcher, **kwargs: Any) -> DiscoveryBatch:
        cutoff = kwargs.get("cutoff")
        progress = kwargs.get("progress")
        page = int((kwargs.get("cursor") or {}).get("page", 0))
        items: list[DiscoveredItem] = []
        reports: list[dict[str, Any]] = []
        reason = "page_batch_complete"
        need_fields = [
            "companyCd", "companyName", "disclosureTitle", "disclosurePostTitle",
            "destFilePath", "publishDate", "xxfcbj", "fileExt", "xxzrlx",
        ]
        for _ in range(self.max_pages):
            fields = [
                ("siteId", "6"), ("flag", "0"), ("disclosureType", ""),
                ("page", str(page)), ("companyCd", ""), ("isNewThree", "1"),
                ("startTime", cutoff.date().isoformat() if cutoff else ""),
                ("endTime", datetime.now(UTC).date().isoformat()), ("keyword", ""),
                ("hyType", ""), ("xxfcbj[]", "2"),
            ] + [("needFields[]", field) for field in need_fields]
            response = fetcher.post_form(
                self.endpoint,
                fields,
                headers={
                    "Referer": source.entry_urls[0],
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            payload = json.loads(response.content)
            content = payload.get("data", {}).get("content", [])
            rows = content[0].get("disclosures", []) if content else []
            found = 0
            for record in rows:
                path = record.get("destFilePath")
                if not path:
                    continue
                items.append(DiscoveredItem(
                    url=canonicalize_url(urljoin("https://www.bse.cn", path)),
                    title=record.get("disclosureTitle"),
                    published_at=_parse_datetime(record.get("publishDate")),
                    external_id=record.get("disclosureCode") or None,
                    metadata={
                        "list_page": self.endpoint,
                        "company_code": record.get("companyCd", ""),
                        "company_name": record.get("companyName", ""),
                    },
                ))
                found += 1
            report = {"url": self.endpoint, "status": "ok", "page": page, "items": found}
            reports.append(report)
            _emit(progress, report)
            page += 1
            if not rows:
                reason = "entries_complete"
                break
            if len(items) >= self.max_items:
                break
        complete = reason != "page_batch_complete"
        return DiscoveryBatch(
            items[: self.max_items],
            {"page": page, "complete": complete, "reason": reason},
            reports,
        )


class BeijingSmeServiceAdapter:
    """Extract static detail IDs rendered by the Beijing SME service portal."""

    detail_pattern = re.compile(r"(?:/detail/|opendetail;richtext;)([a-f0-9]{24})", re.I)

    def __init__(self, *, max_pages: int = 5, max_items: int = 5000) -> None:
        del max_pages
        self.max_items = max_items

    def discover(self, source: Source, fetcher: HttpFetcher, **kwargs: Any) -> DiscoveryBatch:
        progress = kwargs.get("progress")
        cutoff = kwargs.get("cutoff")
        items: list[DiscoveredItem] = []
        seen: set[str] = set()
        reports: list[dict[str, Any]] = []

        for page_url in source.entry_urls:
            response = fetcher.fetch(page_url)
            soup = BeautifulSoup(response.content, "html.parser")
            page_count = 0
            for element in soup.find_all(["a", "li", "div"]):
                target = " ".join(
                    str(element.get(attribute, ""))
                    for attribute in ("href", ":href", "v-bind:href", "@click", "v-on:click")
                )
                match = self.detail_pattern.search(target)
                if not match or match.group(1) in seen:
                    continue
                container = element.find_parent(["li", "article"]) or element
                context = " ".join(container.get_text(" ", strip=True).split())
                published_at = _published_at_from_text(context)
                if cutoff and published_at and published_at < cutoff:
                    continue
                seen.add(match.group(1))
                title_element = element.find(["p", "span", "h2", "h3"])
                title = (
                    " ".join(title_element.get_text(" ", strip=True).split())
                    if title_element
                    else " ".join(element.get_text(" ", strip=True).split())
                )
                items.append(
                    DiscoveredItem(
                        url=f"https://www.smebj.cn/detail/{match.group(1)}.html",
                        title=title or None,
                        published_at=published_at,
                        external_id=match.group(1),
                        metadata={
                            "list_page": page_url,
                            "source_section": _sme_source_section(page_url),
                            "required_selector": ".artDetail_content .content",
                        },
                    )
                )
                page_count += 1
                if len(items) >= self.max_items:
                    break
            report = {"url": page_url, "status": "ok", "items": page_count}
            reports.append(report)
            _emit(progress, report)
            if len(items) >= self.max_items:
                break

        failures = (
            []
            if items
            else [{"url": source.entry_urls[0], "error": "no SME detail IDs found"}]
        )
        return DiscoveryBatch(
            items,
            {"complete": True, "reason": "static_home_complete"},
            reports,
            failures,
        )


def _sme_source_section(page_url: str) -> str:
    path = urlsplit(page_url).path.lower()
    for section in ("policy", "activity", "report", "news"):
        if f"/{section}/" in path:
            return section
    return "other"


class HicoolProjectsApiAdapter:
    """Collect public project cards from HICOOL's own matchmaking API."""

    endpoint = "https://hicool.com/index.php/jointapi/jointprojectlist"

    def __init__(self, *, max_pages: int = 5, max_items: int = 5000) -> None:
        self.max_pages = max_pages
        self.max_items = max_items

    def discover(self, source: Source, fetcher: HttpFetcher, **kwargs: Any) -> DiscoveryBatch:
        cutoff = kwargs.get("cutoff")
        progress = kwargs.get("progress")
        page = int((kwargs.get("cursor") or {}).get("page", 1))
        items: list[DiscoveredItem] = []
        reports: list[dict[str, Any]] = []
        reason = "page_batch_complete"
        for _ in range(self.max_pages):
            page_url = _replace_query(
                self.endpoint,
                territorytype="",
                timetype="",
                group="",
                limit=min(20, self.max_items),
                page=page,
            )
            response = fetcher.fetch(
                page_url,
                headers={"Accept": "application/json", "Referer": source.entry_urls[0]},
            )
            payload = json.loads(response.content)
            rows = payload.get("data") or []
            old_items = 0
            for record in rows:
                project_id = str(record.get("id") or "").strip()
                title = str(record.get("title") or "").strip()
                if not project_id or not title:
                    continue
                published_at = _parse_datetime(record.get("create_time"))
                if cutoff and published_at and published_at < cutoff:
                    old_items += 1
                    continue
                detail_url = _replace_query(
                    "https://hicool.com/index.php/jointapi/jointprojectdetails",
                    id=project_id,
                )
                fields = [
                    ("项目名称", title),
                    ("标签", record.get("tag")),
                    ("所在地区", record.get("city") or record.get("place")),
                    ("赛道", record.get("tracktype")),
                    ("项目介绍", record.get("introduce")),
                    ("产品与服务", record.get("service")),
                    ("商业模式", record.get("pattern")),
                    ("项目结构", record.get("structure")),
                    ("竞争力", record.get("competitiveness")),
                ]
                body = "".join(
                    f"<section><h2>{html.escape(label)}</h2><p>{html.escape(str(value))}</p></section>"
                    for label, value in fields
                    if value
                )
                material = (
                    "<!doctype html><html><head><meta charset=\"utf-8\">"
                    f"<title>{html.escape(title)}</title></head>"
                    f"<body><article><h1>{html.escape(title)}</h1>{body}</article></body></html>"
                ).encode()
                items.append(
                    DiscoveredItem(
                        url=detail_url,
                        title=title,
                        published_at=published_at,
                        external_id=project_id,
                        metadata={"list_page": page_url, "capture_mode": "public_api"},
                        inline_content=material,
                        inline_mime_type="text/html",
                    )
                )
                if len(items) >= self.max_items:
                    break
            report = {
                "url": page_url,
                "status": "ok",
                "page": page,
                "items": len(rows) - old_items,
                "old_items": old_items,
            }
            reports.append(report)
            _emit(progress, report)
            page += 1
            if not rows or old_items:
                reason = "entries_complete" if not rows else "cutoff"
                break
            if len(items) >= self.max_items:
                break
        return DiscoveryBatch(
            items[: self.max_items],
            {"page": page, "complete": reason != "page_batch_complete", "reason": reason},
            reports,
        )


class DatedContextListAdapter:
    """Discover dated article links on association sites with nonstandard URLs."""

    def __init__(self, *, max_pages: int = 5, max_items: int = 5000) -> None:
        self.max_pages = max_pages
        self.max_items = max_items

    def discover(self, source: Source, fetcher: HttpFetcher, **kwargs: Any) -> DiscoveryBatch:
        cutoff = kwargs.get("cutoff")
        progress = kwargs.get("progress")
        items: list[DiscoveredItem] = []
        seen: set[str] = set()
        reports: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []

        for page_url in source.entry_urls[: self.max_pages]:
            try:
                response = fetcher.fetch(page_url)
            except CollectionError as error:
                failures.append({"url": page_url, "error": str(error)})
                continue
            soup = BeautifulSoup(response.content, "html.parser")
            page_count = 0
            for anchor in soup.find_all("a", href=True):
                title = " ".join(anchor.get_text(" ", strip=True).split())
                if len(title) < 5 or title.lower().startswith(("more", "view more")):
                    continue
                try:
                    url = _canonicalize_dated_context_url(
                        urljoin(response.url, str(anchor["href"]))
                    )
                except CollectionError:
                    continue
                if (
                    url in seen
                    or not _same_site(response.url, url)
                    or _is_pagination_link(url, title)
                    or re.search(r"[?&]list_\d+%?2?f?=$", url, re.I)
                ):
                    continue
                container = anchor
                published_at = None
                for _ in range(4):
                    context = " ".join(container.get_text(" ", strip=True).split())
                    published_at = _published_at_from_text(context)
                    if published_at or container.parent is None:
                        break
                    container = container.parent
                if published_at is None:
                    continue
                seen.add(url)
                if cutoff and published_at < cutoff:
                    continue
                items.append(
                    DiscoveredItem(
                        url=url,
                        title=title,
                        published_at=published_at,
                        metadata={"list_page": page_url},
                    )
                )
                page_count += 1
                if len(items) >= self.max_items:
                    break
            report = {"url": page_url, "status": "ok", "items": page_count}
            reports.append(report)
            _emit(progress, report)
            if len(items) >= self.max_items:
                break
        if not items and not failures:
            failures.append({"url": source.entry_urls[0], "error": "no dated links found"})
        return DiscoveryBatch(
            items, {"complete": True, "reason": "entries_complete"}, reports, failures
        )


class SnapshotPageAdapter:
    """Store a public catalogue page whose individual cards have no stable URLs."""

    def __init__(self, *, max_pages: int = 5, max_items: int = 5000) -> None:
        del max_pages, max_items

    def discover(self, source: Source, fetcher: HttpFetcher, **kwargs: Any) -> DiscoveryBatch:
        progress = kwargs.get("progress")
        items: list[DiscoveredItem] = []
        reports: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for page_url in source.entry_urls:
            try:
                response = fetcher.fetch(page_url)
            except CollectionError as error:
                failures.append({"url": page_url, "error": str(error)})
                continue
            if response.mime_type != "text/html" or not response.content.strip():
                failures.append({"url": page_url, "error": "catalogue page is empty or not HTML"})
                continue
            items.append(
                DiscoveredItem(
                    url=canonicalize_url(response.url),
                    title=source.name,
                    metadata={"capture_mode": "page_snapshot"},
                )
            )
            report = {"url": page_url, "status": "ok", "items": 1}
            reports.append(report)
            _emit(progress, report)
        return DiscoveryBatch(
            items, {"complete": True, "reason": "entries_complete"}, reports, failures
        )


def build_adapter(
    source: Source,
    scope: CollectionScope,
    *,
    max_pages: int = 5,
    max_items: int = 5000,
) -> Any:
    del scope
    if source.adapter_key == "stats_national":
        return StatsNationalAdapter(max_pages=max_pages, max_items=max_items)
    if source.adapter_key == "gov_policy_library":
        return GovPolicyLibraryAdapter(max_pages=max_pages, max_items=max_items)
    if source.adapter_key == "beijing_policy":
        return GenericSiteAdapter(max_pages=max_pages, max_items=max_items)
    if source.adapter_key == "fangshan_policy":
        return FangshanPolicyAdapter(max_pages=max_pages, max_items=max_items)
    if source.adapter_key == "haidian_policy_api":
        return HaidianPolicyApiAdapter(max_pages=max_pages, max_items=max_items)
    if source.adapter_key == "standards_samr":
        return StandardsSamrAdapter(max_pages=max_pages, max_items=max_items)
    if source.adapter_key == "bse_disclosure":
        return BseDisclosureAdapter(max_pages=max_pages, max_items=max_items)
    if source.adapter_key == "beijing_sme_service":
        return BeijingSmeServiceAdapter(max_pages=max_pages, max_items=max_items)
    if source.adapter_key == "hicool_projects_api":
        return HicoolProjectsApiAdapter(max_pages=max_pages, max_items=max_items)
    if source.adapter_key == "dated_context_list":
        return DatedContextListAdapter(max_pages=max_pages, max_items=max_items)
    if source.adapter_key == "snapshot_page":
        return SnapshotPageAdapter(max_pages=max_pages, max_items=max_items)
    return GenericSiteAdapter(max_pages=max_pages, max_items=max_items)


def default_cutoff(scope: CollectionScope, now: datetime | None = None) -> datetime:
    cutoff = months_ago(now or datetime.now(UTC), scope.backfill_months)
    return cutoff.replace(hour=0, minute=0, second=0, microsecond=0)


def _canonicalize_dated_context_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.query and "=" not in parts.query and re.fullmatch(
        r"list_\d+/\d+\.html", parts.query, re.I
    ):
        base = canonicalize_url(urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")))
        return f"{base}?{parts.query}"
    return canonicalize_url(url)


def _is_article_link(
    url: str,
    title: str,
    published_at: datetime | None,
    article_url_patterns: list[str] | None = None,
) -> bool:
    normalized_title = " ".join(title.split())
    if not normalized_title or normalized_title in NON_ARTICLE_TITLES:
        return False
    path = urlsplit(url).path.lower()
    if (
        not path.endswith((".html", ".htm", ".shtml", ".pdf", ".action"))
        and "." in path.rsplit("/", 1)[-1]
    ):
        return False
    configured_match = any(
        re.search(pattern, urlsplit(url).path, re.IGNORECASE)
        for pattern in (article_url_patterns or [])
    )
    return bool(
        configured_match
        or
        published_at or ARTICLE_DATE_PATTERN.search(path) or SLUG_ARTICLE_PATTERN.search(path)
    )


def _is_pagination_link(url: str, title: str) -> bool:
    normalized = " ".join(title.split()).lower()
    return normalized in NEXT_PAGE_TITLES or bool(PAGINATION_PATTERN.search(url))


def _select_next_page(
    current_url: str, candidates: list[str], visited: set[str], pending: list[str]
) -> str | None:
    choices = sorted(
        {
            url
            for url in candidates
            if url != current_url and url not in visited and url not in pending
        },
        key=_page_number,
    )
    if not choices:
        return None
    current_page = _page_number(current_url)
    forward = [url for url in choices if _page_number(url) > current_page]
    return forward[0] if forward else None


def _page_number(url: str) -> int:
    match = PAGINATION_PATTERN.search(url)
    if not match:
        return 0 if urlsplit(url).path.lower().endswith("index.html") else 10**9
    return int(next(group for group in match.groups() if group is not None))


def _same_site(left: str, right: str) -> bool:
    left_host = (urlsplit(left).hostname or "").lower()
    right_host = (urlsplit(right).hostname or "").lower()
    return _site_key(left_host) == _site_key(right_host)


def _site_key(host: str) -> str:
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in {"gov.cn", "com.cn", "org.cn", "net.cn"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        match = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", normalized)
        if not match:
            return None
        parsed = datetime(*(int(part) for part in match.groups()))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def _replace_query(url: str, **values: Any) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in values.items()})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _published_at_from_text(text: str) -> datetime | None:
    match = re.search(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?", text)
    if not match:
        return None
    try:
        return datetime(*(int(value) for value in match.groups()), tzinfo=UTC)
    except ValueError:
        return None


def _published_at_from_url_and_text(url: str, text: str) -> datetime | None:
    published_at = published_at_from_url(url) or _published_at_from_text(text)
    if published_at:
        return published_at
    year = re.search(r"/(20\d{2})/", urlsplit(url).path)
    month_day = re.search(r"(?<!\d)(\d{1,2})-(\d{1,2})(?!\d)", text)
    if not year or not month_day:
        return None
    try:
        return datetime(
            int(year.group(1)),
            int(month_day.group(1)),
            int(month_day.group(2)),
            tzinfo=UTC,
        )
    except ValueError:
        return None


def _emit(callback: Callable[[dict[str, Any]], None] | None, report: dict[str, Any]) -> None:
    if callback:
        callback(report)
