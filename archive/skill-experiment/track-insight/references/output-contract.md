# Output Contract

## Workspace

```text
<workspace>/
├── manifest.json
├── checkpoint.json
├── research-plan.md
├── work-packages.json
├── search-tasks.jsonl
├── research-gate.json
├── sources.jsonl
├── pages/
│   ├── index.jsonl
│   ├── <source_id>.md
│   └── attachments/
├── events.jsonl
├── topics.json
├── tracks.json
├── evidence/
│   └── evidence-index.jsonl
├── validation.json
├── report.md
└── dashboard.html
```

Use UTF-8 and LF. Use `snake_case` JSON keys, ISO dates, timezone-aware ISO timestamps, `null` for unknown scalar values, and empty arrays for known-empty collections. IDs are unique in one workspace and remain stable across incremental updates.

## Manifest

`manifest.json` records `schema_version`, `skill_version`, `research_id`, the fixed research purpose, normalized inputs, optional parent research ID, timestamps, status, and counts. Status is `running`, `completed`, `incomplete`, or `failed`.

Set `completed` only after validation passes and both formal deliverables exist.

## Checkpoint

`checkpoint.json` contains `current_stage`, `completed_stages`, `pending_items`, `quality_gaps`, `next_actions`, and `updated_at`.

Stages are `planning`, `discovery`, `acquisition`, `event_extraction`, `topic_synthesis`, `track_synthesis`, `track_analysis`, `validation`, `reporting`, and `completed`.

## Sources

Each `sources.jsonl` object contains `source_id`, URL or local path, canonical URL, title, publisher, domain, source class, URL type, possible publication date, selection decision and reason, `search_task_ids`, discovery method, discoveries, acquisition status, page path, content hash, and error.

Source classes are `government`, `park`, `association`, `company`, `industry_media`, `authoritative_media`, `investment_institution`, and `other`. URL types are `content_page` and `source_entry`. Decisions are `keep`, `maybe`, and `drop`.

## Pages

Each `pages/index.jsonl` object contains `page_id`, `source_id`, requested/final URL, content type, acquisition method, HTTP status, title, observed publication date, content path, optional raw content path, character count, hash, quality status/reasons, relevance review, document type, review regions/industries, content sufficiency, and fetch time. Pending quality states are `reader_required` and `ocr_required`; they must be resolved before Event extraction.

## Formal Deliverables

`report.md` includes scope, cutoff date, method and source coverage, Track overview, detailed Track definitions and boundaries, current signals, enterprise/value-chain analysis, observable features, SMB value chains, cited Events/sources, watchlist, and uncertainties. It excludes hidden reasoning, debugging, and abandoned approaches.

`dashboard.html` is standalone and opens without a database or server. It shows counts, Track status and filtering, Track definitions and enterprise groups, current changes, products/services, observable features, supporting Topics/Events, SMB value chains, and uncertainties.

## Incremental Updates

Validate the parent workspace before use. Reuse stable IDs and unchanged page content. Append new sources and Events. Version materially changed Topics and Tracks with `previous_version_id`. Write the update to a new result directory so the parent remains recoverable.
