import uuid
from typing import Literal

from pydantic import BaseModel, Field


class CandidateTrackOutput(BaseModel):
    name: str = Field(min_length=2, max_length=300)
    definition: str = Field(min_length=10, max_length=2000)
    topic_ids: list[uuid.UUID] = Field(min_length=1)
    status: Literal["candidate", "watchlist"]
    confidence: float = Field(ge=0, le=1)


class TrackBuildOutput(BaseModel):
    tracks: list[CandidateTrackOutput] = Field(default_factory=list)


class CompactCandidateTrackOutput(BaseModel):
    name: str = Field(min_length=2, max_length=300)
    refs: list[str] = Field(min_length=1)


class CompactTrackBuildOutput(BaseModel):
    tracks: list[CompactCandidateTrackOutput] = Field(default_factory=list)


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
