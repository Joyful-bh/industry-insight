import uuid
from typing import Literal

from pydantic import BaseModel, Field, model_validator

SupplierRole = Literal[
    "DESIGN_PRODUCT",
    "MANUFACTURE_PRODUCT",
    "MANUFACTURING_PROCESS_SERVICE",
    "INDUSTRIAL_SOFTWARE",
]


class CandidateTrackOutput(BaseModel):
    name: str = Field(min_length=2, max_length=300)
    offering_family: str = Field(min_length=2, max_length=100)
    supplier_role: SupplierRole
    definition: str = Field(min_length=10, max_length=2000)
    topic_ids: list[uuid.UUID] = Field(min_length=1)
    supporting_topic_ids: list[uuid.UUID] = Field(default_factory=list)
    identity_event_ids: list[uuid.UUID] = Field(default_factory=list)
    status: Literal["candidate", "watchlist"]
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def topic_roles_are_disjoint(self) -> "CandidateTrackOutput":
        if set(self.topic_ids) & set(self.supporting_topic_ids):
            raise ValueError("core and supporting Topic IDs must be disjoint")
        return self


class TrackBuildOutput(BaseModel):
    tracks: list[CandidateTrackOutput] = Field(default_factory=list)


class IdentifiedCandidateTrackOutput(BaseModel):
    name: str = Field(min_length=2, max_length=300)
    offering_family: str = Field(min_length=2, max_length=100)
    supplier_role: SupplierRole
    refs: list[str] = Field(min_length=1)
    supporting_refs: list[str] = Field(default_factory=list)
    core_event_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def topic_roles_are_disjoint(self) -> "IdentifiedCandidateTrackOutput":
        if set(self.refs) & set(self.supporting_refs):
            raise ValueError("core and supporting Topic references must be disjoint")
        return self


class IdentifiedTrackBuildOutput(BaseModel):
    tracks: list[IdentifiedCandidateTrackOutput] = Field(default_factory=list)
    excluded_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def exclude_only_unassigned_topics(cls, value):
        """Exclusion describes wholly unused Topics, not unused parts of mixed Topics."""
        if isinstance(value, dict):
            assigned = {
                ref
                for track in value.get("tracks", [])
                if isinstance(track, dict)
                for ref in [*track.get("refs", []), *track.get("supporting_refs", [])]
            }
            value = dict(value)
            value["excluded_refs"] = list(
                dict.fromkeys(ref for ref in value.get("excluded_refs", []) if ref not in assigned)
            )
        return value

    @model_validator(mode="after")
    def topic_refs_are_unique(self) -> "IdentifiedTrackBuildOutput":
        occurrences = {}
        for track in self.tracks:
            refs = [*track.refs, *track.supporting_refs]
            if len(refs) != len(set(refs)):
                raise ValueError("duplicate Topic reference inside one Track")
            for ref in track.refs:
                previous = occurrences.setdefault(ref, [])
                for other in previous:
                    if (
                        not track.core_event_refs
                        or not other.core_event_refs
                        or set(track.core_event_refs) & set(other.core_event_refs)
                    ):
                        raise ValueError("shared Topic requires distinct core event evidence")
                previous.append(track)
        assigned = {ref for track in self.tracks for ref in [*track.refs, *track.supporting_refs]}
        if (
            len(self.excluded_refs) != len(set(self.excluded_refs))
            or set(self.excluded_refs) & assigned
        ):
            raise ValueError("excluded Topic cannot also be assigned")
        return self


class TrackDefinitionItem(BaseModel):
    track_ref: str
    definition: str = Field(min_length=20, max_length=600)


class TrackDefinitionOutput(BaseModel):
    definitions: list[TrackDefinitionItem]


class ActivityAssessment(BaseModel):
    label: Literal[
        "newly_observed", "recently_active", "continuously_active", "insufficient_history"
    ]
    historical_comparability: bool
    basis: str = Field(min_length=5, max_length=1500)


class SmbValueItem(BaseModel):
    business_activity: str = Field(min_length=2, max_length=500)
    workload: str = Field(min_length=2, max_length=1000)
    it_need: str = Field(min_length=2, max_length=1000)
    coverage_path: str = Field(min_length=2, max_length=1000)


class SmbAssociationItem(BaseModel):
    workload: str = Field(min_length=2, max_length=300)
    product_category: str = Field(min_length=2, max_length=200)
    reason: str = Field(min_length=2, max_length=500)


class SmbAssociationOutput(BaseModel):
    smb_value_analysis: list[SmbAssociationItem] = Field(max_length=3)


class TrackAnalysisOutput(BaseModel):
    enterprise_archetype: str = Field(min_length=10, max_length=1500)
    core_products_services: list[str] = Field(min_length=1, max_length=8)
    core_company_types: list[str] = Field(min_length=1, max_length=8)
    supporting_company_types: list[str] = Field(default_factory=list, max_length=8)
    shared_demand_drivers: list[str] = Field(min_length=1, max_length=8)
    included_activities: list[str] = Field(min_length=1, max_length=8)
    excluded_activities: list[str] = Field(min_length=1, max_length=10)
    chain_roles: list[str] = Field(min_length=1, max_length=8)
    observable_company_features: list[str] = Field(min_length=2, max_length=10)
    possible_it_needs: list[str] = Field(min_length=1, max_length=10)
    summary: str = Field(min_length=10, max_length=3000)
    why_now: str = Field(min_length=10, max_length=3000)
    activity_assessment: ActivityAssessment
    industry_chain_analysis: str = Field(min_length=10, max_length=3000)
    smb_value_analysis: list[SmbValueItem] = Field(min_length=1, max_length=20)
    evidence_event_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    uncertainties: list[str] = Field(default_factory=list, max_length=30)


class TrackStatus(BaseModel):
    plan_id: str
    track_count: int
    analyzed_count: int
    topic_count: int
    assigned_topic_count: int
