from collections import Counter
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from track_insight.core.enums import SearchDecision, TaskStatus
from track_insight.core.errors import ContractError
from track_insight.discovery.contracts import CoverageAudit
from track_insight.infrastructure.models import (
    ResearchPlan,
    ResearchWorkPackage,
    SearchResult,
    SearchTask,
    UrlCandidate,
    UrlDiscovery,
)
from track_insight.settings import PocConfig


def build_coverage_audit(
    session: Session, plan_id: object, config: PocConfig, *, persist: bool = True
) -> CoverageAudit:
    plan = session.get(ResearchPlan, plan_id)
    if plan is None:
        raise ContractError("research plan does not exist")
    packages = list(
        session.scalars(
            select(ResearchWorkPackage).where(ResearchWorkPackage.research_plan_id == plan.id)
        )
    )
    package_ids = [package.id for package in packages]
    tasks = (
        list(
            session.scalars(
                select(SearchTask).where(SearchTask.research_work_package_id.in_(package_ids))
            )
        )
        if package_ids
        else []
    )
    task_ids = [task.id for task in tasks]
    results = (
        list(session.scalars(select(SearchResult).where(SearchResult.search_task_id.in_(task_ids))))
        if task_ids
        else []
    )
    discoveries = (
        list(session.scalars(select(UrlDiscovery).where(UrlDiscovery.search_task_id.in_(task_ids))))
        if task_ids
        else []
    )
    candidate_ids = {item.url_candidate_id for item in discoveries}
    candidates = (
        list(session.scalars(select(UrlCandidate).where(UrlCandidate.id.in_(candidate_ids))))
        if candidate_ids
        else []
    )
    source_counts = Counter(item.source_class for item in candidates)
    domain_counts = Counter(urlsplit(item.canonical_url).hostname or "" for item in candidates)
    missing = sorted(set(config.coverage.required_source_classes) - set(source_counts))
    warnings = [f"missing source class: {value}" for value in missing]
    if candidates and domain_counts:
        domain, count = domain_counts.most_common(1)[0]
        if count / len(candidates) >= config.coverage.dominant_domain_warning_ratio:
            warnings.append(f"dominant domain: {domain} ({count}/{len(candidates)})")
    audit = CoverageAudit(
        plan_id=str(plan.id),
        work_package_count=len(packages),
        completed_work_package_count=sum(
            package.status == TaskStatus.COMPLETED for package in packages
        ),
        search_task_count=len(tasks),
        completed_search_task_count=sum(task.status == TaskStatus.COMPLETED for task in tasks),
        raw_result_count=len(results),
        retained_result_count=sum(
            result.decision in {SearchDecision.KEEP, SearchDecision.MAYBE} for result in results
        ),
        candidate_count=len(candidates),
        distinct_domain_count=len(domain_counts),
        source_class_counts=dict(sorted(source_counts.items())),
        dominant_domains=[
            {"domain": domain, "candidate_count": count}
            for domain, count in domain_counts.most_common(10)
        ],
        missing_source_classes=missing,
        warnings=warnings,
    )
    if persist:
        plan.coverage_audit = audit.model_dump(mode="json")
        session.flush()
    return audit
