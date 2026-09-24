# Search Planning and Discovery Gate

The search plan is a machine-readable execution ledger. Generate it before searching so coverage does not depend on conversation memory.

## WorkPackages

Write `work-packages.json` as:

```json
{
  "schema_version": "work-package-v1",
  "minimum_unique_content_urls": 30,
  "minimum_source_classes": 4,
  "maximum_domain_ratio": 0.4,
  "work_packages": []
}
```

Each WorkPackage contains:

- `work_package_id`, `name`, `purpose`;
- `target_regions`, `target_industries`;
- `target_signal_types`, `target_source_classes`;
- `completion_criteria`;
- `minimum_retained_urls`;
- `status`: `pending` or `completed`.

For broad regional or industrial Track research, normally create 6–12 semantically complete WorkPackages. Cover policy, projects/production, enterprise activity, parks/clusters, industry organizations, technology commercialization, investment, and market change when relevant. Do not create a Cartesian product of every dimension.

The top-level coverage thresholds are the minimum acceptable discovery coverage for this research scope. For broad research, use at least 30 unique retained/uncertain content URLs, at least four source classes, and a maximum dominant-domain ratio of 0.4. Increase these values for wider multi-region or multi-industry scopes. Do not lower them after seeing weak results merely to pass the gate.

## SearchTasks

Write one JSON object per line to `search-tasks.jsonl`:

- `search_task_id`, `work_package_id`;
- `query`, `purpose`;
- `target_regions`, `target_industries`;
- `target_signal_types`, `target_source_classes`;
- `status`: `pending`, `completed`, or `failed`;
- `searched_at`;
- `result_count`;
- `discovered_source_ids`;
- `error`.

Plan 2–5 distinct queries per WorkPackage. Queries should vary the signal and source perspective, not merely rephrase the same words.

Execute every planned query. A completed query may have zero retained results, but it must record `result_count`, an empty `discovered_source_ids`, and a concise explanation in `error` or `completion_note`. A failed query remains incomplete and blocks the next stage until retried or replaced by a newly recorded SearchTask that satisfies the same purpose.

## Source Linkage

Every Source discovered through search contains `search_task_ids`, listing one or more existing SearchTasks. Preserve multiple discoveries of the same canonical URL by appending task IDs rather than duplicating the Source.

For every `keep` or `maybe` Source whose `url_type` is `content_page`, create a Page acquisition record. Acquisition may finish as:

- `usable`: readable text is saved;
- `insufficient`: fetched content is not useful enough;
- `inaccessible`: access failed or is prohibited;
- `reader_required`: static fetch failed and the URL awaits browser/web-reader processing;
- `ocr_required`: a downloaded document awaits OCR.

`reader_required` and `ocr_required` are pending states and block Event extraction. `insufficient` and `inaccessible` count as completed acquisition attempts but reduce effective coverage.

## Advancement Gate

Run `scripts/check_research_gate.py <workspace> --write` before Event extraction. The gate requires:

- every WorkPackage is completed;
- every SearchTask is completed;
- every WorkPackage meets its retained URL minimum;
- all Source references resolve to executed SearchTasks;
- every retained/uncertain content page has a terminal acquisition record;
- the workspace meets its unique-URL, source-class, and domain-concentration thresholds.

The gate writes `research-gate.json`. Do not create Events while it reports `passed: false`.
