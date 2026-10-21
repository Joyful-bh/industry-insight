"""Rule-based readiness of a track's existing direct identity evidence."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import select

from track_insight.core.fingerprints import fingerprint
from track_insight.infrastructure.models import Event, EventEvidence, PageCapture

SCORING_VERSION = "identity-evidence-rules-v2"
MIN_EXTRACTION_CONFIDENCE = 0.70
MEDIA_DOMAINS = ("xinhuanet.com", "news.cn", "people.com.cn", "cctv.com", "cnr.cn")


def source_level(url):
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host == "gov.cn" or host.endswith(".gov.cn"):
        return host, "government"
    if any(host == domain or host.endswith("." + domain) for domain in MEDIA_DOMAINS):
        return host, "authoritative_media"
    return host, "other"


@dataclass(frozen=True)
class EvidenceFact:
    event_id: str
    fingerprint: str
    quote_hash: str
    document: str
    host: str
    level: str
    extraction_confidence: float
    has_quote: bool


def score_evidence(facts):
    """Repeated facts, identical excerpts and the same body cannot supply corroboration."""
    qualifying = [
        f
        for f in facts
        if f.has_quote and f.host and f.extraction_confidence >= MIN_EXTRACTION_CONFIDENCE
    ]
    authoritative = [f for f in qualifying if f.level in {"government", "authoritative_media"}]
    selected, seen_facts, seen_quotes, seen_documents = [], set(), set(), set()
    for fact in sorted(
        qualifying,
        key=lambda f: (
            f.level not in {"government", "authoritative_media"},
            -f.extraction_confidence,
            f.event_id,
            f.document,
        ),
    ):
        event_key = fact.fingerprint or fact.event_id
        if (
            event_key in seen_facts
            or fact.quote_hash in seen_quotes
            or fact.document in seen_documents
        ):
            continue
        selected.append(fact)
        seen_facts.add(event_key)
        seen_quotes.add(fact.quote_hash)
        seen_documents.add(fact.document)
    if authoritative:
        status, reason = "candidate", "authoritative_direct_evidence"
    elif len(selected) >= 2:
        status, reason = "candidate", "multiple_distinct_direct_evidence"
    else:
        status, reason = "watchlist", "insufficient_direct_evidence"
    return dict(
        version=SCORING_VERSION,
        status=status,
        reason=reason,
        cited_events=len({f.event_id for f in facts}),
        qualifying_events=len({f.event_id for f in qualifying}),
        authoritative_events=len({f.event_id for f in authoritative}),
        independent_evidence_units=len(selected),
        document_levels=dict(Counter(f.level for f in selected)),
        accepted_event_ids=sorted({f.event_id for f in selected}),
        min_extraction_confidence=MIN_EXTRACTION_CONFIDENCE,
    )


def recalculate_evidence_strength(session, tracks):
    """Only existing identity references count; this does not reassess semantic coverage."""
    ids = {eid for track in tracks for eid in track.identity_event_ids}
    events = (
        {e.id: e for e in session.scalars(select(Event).where(Event.id.in_(ids)))} if ids else {}
    )
    facts = defaultdict(list)
    rows = (
        session.execute(
            select(EventEvidence, PageCapture)
            .join(PageCapture, PageCapture.id == EventEvidence.page_capture_id)
            .where(EventEvidence.event_id.in_(ids))
        )
        if ids
        else []
    )
    for evidence, capture in rows:
        event = events[evidence.event_id]
        host, level = source_level(
            evidence.source_url or capture.final_url or capture.requested_url
        )
        normalized_quote = "".join((evidence.evidence_text or "").split())
        facts[event.id].append(
            EvidenceFact(
                event_id=str(event.id),
                fingerprint=event.event_fingerprint,
                quote_hash=fingerprint(normalized_quote),
                document=capture.content_hash or str(capture.id),
                host=host,
                level=level,
                extraction_confidence=event.confidence,
                has_quote=bool(normalized_quote),
            )
        )
    audit = []
    for index, track in enumerate(tracks):
        old = dict(status=track.status, confidence=track.confidence)
        score = score_evidence(
            [fact for eid in set(track.identity_event_ids) for fact in facts.get(eid, [])]
        )
        track.status = score["status"]
        audit.append(
            dict(
                index=index,
                name=track.name,
                previous=old,
                missing_event_ids=sorted(
                    str(eid) for eid in track.identity_event_ids if eid not in events
                ),
                **score,
            )
        )
    return audit
