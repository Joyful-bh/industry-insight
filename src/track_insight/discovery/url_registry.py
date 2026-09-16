from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from track_insight.core.enums import SearchDecision
from track_insight.core.errors import ContractError
from track_insight.infrastructure.models import (
    SearchResult,
    UrlCandidate,
    UrlDiscovery,
)

TRACKING_PARAMETERS = {
    "from",
    "spm",
    "source",
    "ref",
    "referrer",
    "campaign",
    "mc_cid",
    "mc_eid",
}


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ContractError(f"unsupported URL: {url}")
    host = parts.hostname.lower()
    if parts.port and not (
        (parts.scheme.lower() == "http" and parts.port == 80)
        or (parts.scheme.lower() == "https" and parts.port == 443)
    ):
        host = f"{host}:{parts.port}"
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_PARAMETERS:
            continue
        query.append((key, value))
    normalized_path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), host, normalized_path, urlencode(sorted(query)), ""))


def register_candidate(
    session: Session,
    *,
    work_package_id: object,
    search_task_id: object,
    search_result: SearchResult,
) -> UrlCandidate:
    canonical_url = canonicalize_url(search_result.url)
    candidate = session.scalar(
        select(UrlCandidate).where(UrlCandidate.canonical_url == canonical_url)
    )
    if candidate is None:
        candidate = UrlCandidate(
            canonical_url=canonical_url,
            url_type=search_result.url_type,
            title=search_result.title,
            snippet=search_result.snippet,
            source_domain=urlsplit(canonical_url).hostname or "",
            source_class=search_result.source_class,
            possible_published_at=search_result.publish_date,
            decision=search_result.decision,
        )
        session.add(candidate)
        session.flush()
    elif (
        candidate.decision == SearchDecision.MAYBE and search_result.decision == SearchDecision.KEEP
    ):
        candidate.decision = SearchDecision.KEEP
    existing = session.scalar(
        select(UrlDiscovery).where(
            UrlDiscovery.url_candidate_id == candidate.id,
            UrlDiscovery.research_work_package_id == work_package_id,
            UrlDiscovery.search_task_id == search_task_id,
        )
    )
    if existing is None:
        session.add(
            UrlDiscovery(
                url_candidate_id=candidate.id,
                research_work_package_id=work_package_id,
                search_task_id=search_task_id,
                search_result_id=search_result.id,
            )
        )
    session.flush()
    return candidate
