from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from track_insight.core.errors import ContractError
from track_insight.tracks.contracts import CandidateTrackOutput, SupplierRole


class Market(BaseModel):
    market_ref: str = Field(min_length=1)
    name: str = Field(min_length=2, max_length=300)
    offering_family: str = Field(min_length=2, max_length=100)
    supplier_role: SupplierRole
    boundary: str = Field(min_length=10, max_length=2000)
    member_refs: list[str] = Field(min_length=2)


class MarketCatalog(BaseModel):
    markets: list[Market] = Field(default_factory=list)


class DirectMarketCatalog(MarketCatalog):
    """Coarse market outputs may share a source describing several business groups."""


class MarketDecision(BaseModel):
    group_ref: str
    decision: Literal["ACCEPT", "REJECT"]


class MarketDecisions(BaseModel):
    decisions: list[MarketDecision] = Field(default_factory=list)


def _check_refs(actual: list[str], expected: set[str]) -> None:
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ContractError("market normalization must account for every input reference once")


def market_output_model(task, payload, schema):
    @model_validator(mode="after")
    def validate_batch(self):
        if task in {"catalog", "direct_catalog"}:
            expected = {track["track_ref"] for track in payload["tracks"]}
            names, identities, claimed = set(), set(), set()
            for market in self.markets:
                if market.market_ref in names | {"KEEP", "EXCLUDE"}:
                    raise ValueError(f"Duplicate/reserved market_ref: {market.market_ref}")
                names.add(market.market_ref)
                identity = (
                    market.offering_family.replace(" ", "").casefold(),
                    market.supplier_role,
                )
                if identity in identities:
                    raise ValueError(f"Duplicate product and role identity: {identity}")
                identities.add(identity)
                members = set(market.member_refs)
                if len(members) != len(market.member_refs) or not members <= expected:
                    raise ValueError(
                        f"Market {market.market_ref}: duplicate/unknown member_refs; "
                        f"unknown={sorted(members - expected)}"
                    )
                if members & claimed and task != "direct_catalog":
                    raise ValueError(
                        f"Candidates assigned to multiple markets: {sorted(members & claimed)}"
                    )
                claimed.update(members)
        else:
            expected = {group["group_ref"] for group in payload["groups"]}
            actual = [decision.group_ref for decision in self.decisions]
            if len(actual) != len(set(actual)) or set(actual) != expected:
                raise ValueError(
                    f"Audit each group exactly once; missing={sorted(expected - set(actual))}, "
                    f"unknown={sorted(set(actual) - expected)}, duplicates present="
                    f"{len(actual) != len(set(actual))}"
                )
        return self

    return type(schema.__name__, (schema,), {"validate_batch": validate_batch})


def normalize_markets(
    tracks: list[CandidateTrackOutput],
    scope: dict[str, Any],
    complete: Callable,
    map_batches: Callable | None = None,
) -> tuple[list[CandidateTrackOutput], dict[str, int]]:
    """Build a run-local catalog; preserve every source on rejected or unsafe merges."""
    if len(tracks) < 2:
        return tracks, {
            "input_tracks": len(tracks),
            "output_tracks": len(tracks),
            "merged_groups": 0,
        }
    sources = {f"C{i}": track for i, track in enumerate(tracks)}
    payloads = {
        ref: dict(
            track_ref=ref,
            name=track.name,
            offering_family=track.offering_family,
            supplier_role=track.supplier_role,
            definition=track.definition,
        )
        for ref, track in sources.items()
    }
    catalog = complete(
        "catalog", {"scope": scope, "tracks": list(payloads.values())}, MarketCatalog
    )
    markets = {market.market_ref: market for market in catalog.markets}
    if len(markets) != len(catalog.markets) or {"KEEP", "EXCLUDE"} & set(markets):
        raise ContractError("market catalog references must be unique and not reserved")
    identities = [
        (m.offering_family.replace(" ", "").casefold(), m.supplier_role) for m in markets.values()
    ]
    if len(identities) != len(set(identities)):
        raise ContractError("market catalog contains duplicate product and role identities")
    if not markets:
        return tracks, {
            "input_tracks": len(tracks),
            "output_tracks": len(tracks),
            "merged_groups": 0,
        }
    groups = {}
    claimed = set()
    blocked = 0
    for market in catalog.markets:
        members = market.member_refs
        if len(members) != len(set(members)) or not set(members) <= set(sources):
            raise ContractError("market members must be distinct existing candidate references")
        if claimed.intersection(members):
            raise ContractError("candidate appears in more than one proposed market")
        claimed.update(members)
        if any(sources[ref].supplier_role != market.supplier_role for ref in members):
            blocked += 1
            continue
        groups[market.market_ref] = members
    proposed = [
        {
            "group_ref": ref,
            "proposed_market": markets[ref].model_dump(),
            "candidates": [payloads[member] for member in members],
        }
        for ref, members in groups.items()
        if len(members) > 1
    ]
    decisions = {}
    if proposed:
        audit = complete("audit", {"scope": scope, "groups": proposed}, MarketDecisions)
        _check_refs([d.group_ref for d in audit.decisions], {g["group_ref"] for g in proposed})
        decisions = {d.group_ref: d.decision for d in audit.decisions}
    replacements = {}
    consumed = set()
    merged_groups = 0
    for ref, members in groups.items():
        if len(members) < 2 or decisions.get(ref) != "ACCEPT":
            continue
        market = markets[ref]
        core = list(dict.fromkeys(x for member in members for x in sources[member].topic_ids))
        support = list(
            dict.fromkeys(
                x
                for member in members
                for x in sources[member].supporting_topic_ids
                if x not in core
            )
        )
        replacements[members[0]] = CandidateTrackOutput(
            name=market.name,
            offering_family=market.offering_family,
            supplier_role=market.supplier_role,
            definition=market.boundary,
            topic_ids=core,
            supporting_topic_ids=support,
            identity_event_ids=list(
                dict.fromkeys(
                    eid for member in members for eid in sources[member].identity_event_ids
                )
            ),
            status="watchlist"
            if any(sources[x].status == "watchlist" for x in members)
            else "candidate",
            confidence=min(sources[x].confidence for x in members),
        )
        consumed.update(members)
        merged_groups += 1
    result = [
        replacements[ref] if ref in replacements else track
        for ref, track in sources.items()
        if ref not in consumed or ref in replacements
    ]
    before = {x for t in tracks for x in [*t.topic_ids, *t.supporting_topic_ids]}
    after = {x for t in result for x in [*t.topic_ids, *t.supporting_topic_ids]}
    if before != after:
        raise ContractError("market normalization changed source Topic accounting")
    return result, {
        "input_tracks": len(tracks),
        "output_tracks": len(result),
        "merged_groups": merged_groups,
        "cross_role_assignments_blocked": blocked,
        "proposed_merge_groups": len(catalog.markets),
    }
