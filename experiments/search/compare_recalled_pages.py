"""Read-only inspection of historical recall URLs; no new search/model calls."""

import json
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import select

from track_insight.content.html_text import extract_html_text
from track_insight.infrastructure.database import session_scope
from track_insight.infrastructure.models import SearchResult, SearchTask


def main():
    rng = random.Random(20261009)
    samples = []
    groups = [
        ("bailian_national", "00cf2b85-4b3b-4b22-87df-2755f66b1408", 33),
        ("bailian_beijing_projects", "33da71cf-cada-450c-a806-a17b4f2db651", 20),
        ("bailian_beijing_policy", "87d7d147-d10e-4892-aad1-2f00b05c131b", 10),
    ]
    with session_scope() as session:
        for group, task_id, count in groups:
            task = session.get(SearchTask, UUID(task_id))
            rows = list(
                session.scalars(
                    select(SearchResult).where(SearchResult.search_task_id == task.id)
                ).all()
            )
            rows.sort(key=lambda r: int(r.refer.split("_")[-1]))
            selected = rng.sample(rows, min(count, len(rows)))
            selected.sort(key=lambda r: int(r.refer.split("_")[-1]))
            samples.extend(
                {
                    "group": group,
                    "query": task.query,
                    "refer": r.refer,
                    "url": r.url,
                    "search_title": r.title,
                    "search_snippet": r.snippet,
                    "search_date": r.publish_date,
                }
                for r in selected
            )
    batch = json.loads(
        Path("data/track_quality/opensearch_small_batch/run_1/results.json").read_text(
            encoding="utf-8"
        )
    )
    for task in batch["tasks"]:
        group = (
            "opensearch_beijing" if task["regions"] == ["北京市"] else "opensearch_other_regions"
        )
        rows = task["results"] if group == "opensearch_beijing" else rng.sample(task["results"], 5)
        samples.extend(
            {
                "group": group,
                "query": task["query"],
                "refer": r["refer"],
                "url": r["url"],
                "search_title": r["title"],
                "search_snippet": r["snippet"],
                "search_date": r["publish_date"],
            }
            for r in rows
        )
    policy = json.loads(
        Path("data/track_quality/opensearch_small_batch/run_1/policy_query_check.json").read_text(
            encoding="utf-8"
        )
    )
    samples.extend(
        {
            "group": "opensearch_policy",
            "query": policy["task"]["query"],
            "refer": r["refer"],
            "url": r["link"],
            "search_title": r["title"],
            "search_snippet": r["content"],
            "search_date": r["publish_date"],
        }
        for r in policy["sources"]
    )
    output = Path("data/track_quality/search_recall_comparison/pages.json")
    output.parent.mkdir(parents=True, exist_ok=True)

    def fetch(row):
        try:
            response = httpx.get(
                row["url"], follow_redirects=True, timeout=15, headers={"User-Agent": "Mozilla/5.0"}
            )
            row.update(
                status=response.status_code,
                final_url=str(response.url),
                content_type=response.headers.get("content-type"),
            )
            if response.status_code == 200 and "html" in (row["content_type"] or ""):
                page = extract_html_text(response.content, row["content_type"])
                row.update(
                    page_title=page["title"],
                    page_date=str(page["published_at"]) if page["published_at"] else None,
                    text_length=len(page["content_text"]),
                    text=page["content_text"][:16000],
                )
        except Exception as error:
            row["error"] = f"{type(error).__name__}: {error}"
        return row

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = []
        for row in pool.map(fetch, samples):
            results.append(row)
            if len(results) % 10 == 0:
                print(f"Visited {len(results)}/{len(samples)}", flush=True)
    output.write_text(
        json.dumps({"seed": 20261009, "pages": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(output)


if __name__ == "__main__":
    main()
