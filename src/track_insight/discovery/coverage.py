import re
from collections import Counter
from datetime import date
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
from track_insight.planning.coverage import query_is_regional
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
    region_coverage = {}
    result_by_task = {}
    for result in results:
        result_by_task.setdefault(result.search_task_id, []).append(result)
    for region in plan.regions:
        regional = [
            task
            for task in tasks
            if task.target_regions == [region] and query_is_regional(task.query, region)
        ]
        retained = [
            result
            for task in regional
            for result in result_by_task.get(task.id, [])
            if result.decision in {SearchDecision.KEEP, SearchDecision.MAYBE}
            and result.url_type == "content_page"
        ]
        dates = []
        for result in retained:
            match = re.search(r"\d{4}-\d{2}-\d{2}", result.publish_date or "")
            if match:
                try:
                    dates.append(date.fromisoformat(match[0]))
                except ValueError:
                    pass
        middle = plan.start_date + (plan.end_date - plan.start_date) // 2
        first = sum(plan.start_date <= value <= middle for value in dates)
        second = sum(middle < value <= plan.end_date for value in dates)
        reasons = []
        if not retained:
            reasons.append("no_retained_content")
        elif not any(result.source_class == "government" for result in retained):
            reasons.append("no_government_content")
        if first + second >= 4 and (first == 0 or second == 0):
            reasons.append("time_distribution_gap")
        row = {
            "search_tasks": len(regional),
            "completed_searches": sum(task.status == TaskStatus.COMPLETED for task in regional),
            "failed_searches": sum(str(task.status).startswith("failed") for task in regional),
            "budget_stopped_searches": sum(
                task.completion_reason == "candidate_budget_reached" for task in regional
            ),
            "retained_content_urls": len({result.url for result in retained}),
            "first_half_dated_results": first,
            "second_half_dated_results": second,
            "unknown_publication_dates": len(retained) - len(dates),
            "supplement_reasons": reasons,
        }
        region_coverage[region] = row
        warnings.extend(f"regional coverage {region}: {reason}" for reason in reasons)
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
        region_coverage=region_coverage,
    )
    if persist:
        plan.coverage_audit = audit.model_dump(mode="json")
        session.flush()
    return audit
