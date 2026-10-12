from pydantic import BaseModel, Field, model_validator

from track_insight.core.errors import ContractError
from track_insight.tracks.contracts import IdentifiedTrackBuildOutput


class SupportingAssignment(BaseModel):
    topic_ref: str = Field(min_length=1)
    track_ref: str = Field(min_length=1)
    evidence_event_id: str = Field(min_length=1)
    target_phrase: str = Field(min_length=2)


class SupportingRecoveryOutput(BaseModel):
    assignments: list[SupportingAssignment] = Field(default_factory=list)
    excluded_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def collapse_repeated_evidence(self):
        unique = {}
        for assignment in self.assignments:
            previous = unique.get(assignment.topic_ref)
            if previous is not None and previous.track_ref != assignment.track_ref:
                raise ValueError("one Topic cannot support two different Tracks")
            unique.setdefault(assignment.topic_ref, assignment)
        self.assignments = list(unique.values())
        return self


def support_recovery_model(expected_refs, target_refs):
    """Check batch references inside the client's bounded repair cycle."""

    @model_validator(mode="after")
    def validate_batch(self):
        processed = [x.topic_ref for x in self.assignments] + self.excluded_refs
        actual = set(processed)
        duplicates = sorted(ref for ref in actual if processed.count(ref) > 1)
        if duplicates or actual != expected_refs:
            raise ValueError(
                f"Process each input Topic exactly once; missing={sorted(expected_refs - actual)}, "
                f"unknown={sorted(actual - expected_refs)}, duplicates={duplicates}"
            )
        unknown = sorted({x.track_ref for x in self.assignments} - target_refs)
        if unknown:
            raise ValueError(f"Unknown target Track refs: {unknown}; use only input track_ref")
        return self

    return type(
        "SupportingRecoveryOutput", (SupportingRecoveryOutput,), {"validate_batch": validate_batch}
    )


def apply_support_recovery(
    tracks: IdentifiedTrackBuildOutput,
    recovery: SupportingRecoveryOutput,
    expected_refs: set[str],
    topic_payloads: list[dict],
) -> None:
    targets = {f"R{i}": track for i, track in enumerate(tracks.tracks)}
    processed = [x.topic_ref for x in recovery.assignments] + recovery.excluded_refs
    occupied = {ref for track in tracks.tracks for ref in [*track.refs, *track.supporting_refs]}
    if len(processed) != len(set(processed)) or set(processed) != expected_refs:
        raise ContractError("support recovery must account for every input Topic exactly once")
    if occupied & expected_refs:
        raise ContractError("support recovery cannot reassign an already used Topic")
    if any(x.track_ref not in targets for x in recovery.assignments):
        raise ContractError("support recovery references an unknown Track")
    topics = {topic["topic_ref"]: topic for topic in topic_payloads}
    rejected = []
    generic = {
        "制造",
        "产业",
        "服务",
        "系统",
        "产品",
        "设备",
        "软件",
        "技术",
        "材料",
        "研发",
        "智能",
        "新型",
        "高端",
        "先进",
        "创新",
        "建设",
        "平台",
        "应用",
        "科技",
    }
    for assignment in recovery.assignments:
        track = targets[assignment.track_ref]
        phrase = assignment.target_phrase.strip().casefold()
        events = topics[assignment.topic_ref].get("representative_events", [])
        source = next(
            (
                event
                for event in events
                if str(event.get("event_id")) == assignment.evidence_event_id
            ),
            None,
        )
        in_source = source is not None and any(
            phrase in str(source.get(field, "")).casefold()
            for field in ("title", "summary", "quote")
        )
        # Drop route/grade modifiers only when a non-generic literal product anchor remains.
        anchor = next(
            (
                phrase[start : start + size]
                for size in range(len(phrase), 1, -1)
                for start in range(len(phrase) - size + 1)
                if phrase[start : start + size] not in generic
                and (
                    phrase[start : start + size] in track.name.casefold()
                    or phrase[start : start + size] in track.offering_family.casefold()
                )
            ),
            None,
        )
        if len(phrase) < 2 or phrase in generic or not in_source or not anchor:
            rejected.append(assignment.topic_ref)
            continue
        track.supporting_refs.append(assignment.topic_ref)
    tracks.excluded_refs = (
        [ref for ref in tracks.excluded_refs if ref not in expected_refs]
        + recovery.excluded_refs
        + rejected
    )
