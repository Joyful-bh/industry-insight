import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from track_insight.core.errors import ContractError
from track_insight.planning.contracts import PlanningOutput, ResearchScope

PROVINCES = (
    "北京市",
    "天津市",
    "河北省",
    "山西省",
    "内蒙古自治区",
    "辽宁省",
    "吉林省",
    "黑龙江省",
    "上海市",
    "江苏省",
    "浙江省",
    "安徽省",
    "福建省",
    "江西省",
    "山东省",
    "河南省",
    "湖北省",
    "湖南省",
    "广东省",
    "广西壮族自治区",
    "海南省",
    "重庆市",
    "四川省",
    "贵州省",
    "云南省",
    "西藏自治区",
    "陕西省",
    "甘肃省",
    "青海省",
    "宁夏回族自治区",
    "新疆维吾尔自治区",
)


def expand_scope(scope: ResearchScope) -> ResearchScope:
    regions = list(dict.fromkeys(scope.regions))
    if "全国" in regions:
        regions = list(dict.fromkeys(["全国", *PROVINCES, *regions]))
    return scope.model_copy(update={"regions": regions})


def region_alias(region: str) -> str:
    for suffix in ("壮族自治区", "回族自治区", "维吾尔自治区", "自治区", "省", "市"):
        if region.endswith(suffix):
            return region[: -len(suffix)]
    return region


def query_is_regional(query: str, region: str) -> bool:
    if region == "全国":
        return any(word in query for word in ("全国", "国务院", "国家", "中央", "工信部"))
    return region_alias(region) in query


def time_windows(scope: ResearchScope) -> list[str]:
    middle = scope.start_date + (scope.end_date - scope.start_date) // 2
    second_start = min(middle + timedelta(days=1), scope.end_date).isoformat()
    return list(
        dict.fromkeys(
            [
                f"{scope.start_date.isoformat()}至{middle.isoformat()}",
                f"{second_start}至{scope.end_date.isoformat()}",
            ]
        )
    )


def missing_regions(output: PlanningOutput, regions: list[str]) -> list[str]:
    covered = {
        task.target_regions[0]
        for package in output.work_packages
        for task in package.search_tasks
        if len(task.target_regions) == 1 and query_is_regional(task.query, task.target_regions[0])
    }
    return [region for region in regions if region not in covered]


def validate_batch(
    output: PlanningOutput, regions: list[str], scope: ResearchScope, task_limit: int
):
    if len(output.work_packages) != len(regions):
        raise ContractError("coverage planning requires one work package per assigned region")
    if {tuple(p.regions) for p in output.work_packages} != {(r,) for r in regions}:
        raise ContractError("coverage planning omitted, repeated or combined regions")
    for package in output.work_packages:
        if not 4 <= len(package.search_tasks) <= task_limit:
            raise ContractError("regional planning must respect its 4-to-limit task budget")
        if not any(
            "government" in [s.value for s in t.target_source_classes] for t in package.search_tasks
        ):
            raise ContractError("regional planning requires a government-source task")
        queries = [t.query for t in package.search_tasks]
        if any(not any(window in query for query in queries) for window in time_windows(scope)):
            raise ContractError("regional planning omitted a required time segment")
        for task in package.search_tasks:
            if task.target_regions != package.regions or not query_is_regional(
                task.query, package.regions[0]
            ):
                raise ContractError("query must explicitly target exactly its assigned region")
            if not any(
                str(year) in task.query
                for year in range(scope.start_date.year, scope.end_date.year + 1)
            ):
                raise ContractError("query must include a research-window year")


def apply_query_scope(output, scope):
    """Own geographical and time framing in code; the model selects research content."""
    for package in output.work_packages:
        if len(package.regions) != 1:
            continue
        region = package.regions[0]
        for index, task in enumerate(package.search_tasks):
            query = task.query.strip()
            query = re.sub(
                r"\d{4}年\d{1,2}月至(?:\d{4}年)?\d{1,2}月|"
                r"\d{4}-\d{2}-\d{2}至\d{4}-\d{2}-\d{2}|"
                r"\d{4}年\d{1,2}月(?:\d{1,2}日)?|\d{4}-\d{2}-\d{2}|"
                r"(?<!\d)(?:19|20)\d{2}年?",
                "",
                query,
            ).strip()
            window = (
                time_windows(scope)[index]
                if index < len(time_windows(scope))
                else f"{scope.start_date.isoformat()}至{scope.end_date.isoformat()}"
            )
            query = window + " " + query
            if not query_is_regional(query, region):
                query = region_alias(region) + " " + query
            if len(query) > 70:
                raise ContractError("scoped query exceeds 70 characters")
            task.query = query


def generate_covered_plan(scope, config, complete):
    scope = expand_scope(scope)
    if len(scope.regions) > config.budgets.max_work_packages:
        raise ContractError(
            "work-package budget cannot cover all regions; increase max_work_packages"
        )
    task_limit = min(6, config.budgets.max_searches_per_work_package)
    if task_limit < 4:
        raise ContractError("coverage planning needs at least four searches per work package")
    batches = [scope.regions[i : i + 4] for i in range(0, len(scope.regions), 4)]

    def generate(item):
        index, regions = item
        payload = {
            "region_refs": {f"G{i}": region for i, region in enumerate(regions, 1)},
            "scope": scope.model_copy(update={"regions": regions}).model_dump(mode="json"),
            "coverage_requirements": {
                "one_package_per_region": True,
                "time_segments": time_windows(scope),
                "minimum_tasks_per_region": 4,
                "maximum_tasks_per_region": task_limit,
            },
            "required_source_classes": config.coverage.required_source_classes,
        }
        result = complete(f"batch_{index}", payload)
        attempts = [result]
        try:
            apply_query_scope(result.output, scope)
            validate_batch(result.output, regions, scope, task_limit)
        except ContractError as error:
            result = complete(
                f"batch_{index}_repair",
                {
                    **payload,
                    "validation_error": str(error),
                    "previous_output": result.output.model_dump(mode="json"),
                },
            )
            attempts.append(result)
            apply_query_scope(result.output, scope)
            validate_batch(result.output, regions, scope, task_limit)
        return result, attempts

    with ThreadPoolExecutor(max_workers=config.bailian.max_concurrency) as pool:
        generated = list(pool.map(generate, enumerate(batches)))
    output = PlanningOutput(
        work_packages=[
            package for result, _ in generated for package in result.output.work_packages
        ]
    )
    if missing_regions(output, scope.regions):
        raise ContractError("planning did not cover all mandatory regions")
    return output, [result for _, attempts in generated for result in attempts]


def select_supplement_regions(coverage, limit):
    eligible = [
        (region, row)
        for region, row in coverage.items()
        if row.get("supplement_reasons")
        and not row.get("failed_searches")
        and not row.get("budget_stopped_searches")
    ]
    eligible.sort(
        key=lambda item: (
            item[1].get("retained_content_urls", 0),
            -len(item[1].get("supplement_reasons", [])),
        )
    )
    return [region for region, _ in eligible[: max(0, limit)]]


def validate_supplement(output, regions, existing, start_year, end_year):
    if len(output.work_packages) != len(regions) or {
        tuple(p.regions) for p in output.work_packages
    } != {(r,) for r in regions}:
        raise ContractError("supplement must cover each requested region exactly once")
    queries = {q.strip().casefold() for q in existing}
    for package in output.work_packages:
        if len(package.search_tasks) != 2:
            raise ContractError("supplement requires exactly two new queries per region")
        for task in package.search_tasks:
            key = task.query.strip().casefold()
            if (
                key in queries
                or task.target_regions != package.regions
                or not query_is_regional(task.query, package.regions[0])
            ):
                raise ContractError("supplement query is duplicate or outside its assigned region")
            if not any(str(y) in task.query for y in range(start_year, end_year + 1)):
                raise ContractError("supplement query omitted the research-window year")
            queries.add(key)
