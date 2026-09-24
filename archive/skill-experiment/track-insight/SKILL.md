---
name: track-insight
description: Research policy and industry signals for a user-specified topic, region, and time range; produce evidence-backed tracks suitable for Lenovo SMB business. Use for new or incremental track research, source ingestion, Event/Topic/Track synthesis, and report/dashboard generation. Do not use for company-level customer tagging, contact discovery, CRM opportunity creation, or outreach.
---

# Track Insight

Produce a self-contained research workspace whose formal conclusions trace to page text. The fixed business objective is to identify priority tracks suitable for Lenovo SMB business.

## Inputs

Require only:

- research topic;
- one or more target regions;
- an absolute or relative time range.

Accept optionally:

- specified websites or local files;
- excluded directions;
- expected track count;
- whether to incrementally update an existing result directory.

Resolve relative dates to absolute dates in `manifest.json`. Treat the expected count as a preference, never as a quota. If incremental update is requested without a usable existing result path, ask for that path before changing files.

## Runtime

Resolve `<skill-dir>` as the directory containing this `SKILL.md`; never assume a repository layout, current working directory, user name, drive letter, or installation path. Run scripts with ordinary Python 3.10 or later. The scripts do not require `uv`, Docker, or a project virtual environment.

If the packages declared in `<skill-dir>/requirements.txt` are not already available, install them into the user's chosen Python environment before running acquisition scripts:

```text
python -m pip install -r "<skill-dir>/requirements.txt"
```

## Start or Resume

For a new run, initialize a workspace:

```text
python "<skill-dir>/scripts/init_workspace.py" --topic "<topic>" --region "<region>" --start-date YYYY-MM-DD --end-date YYYY-MM-DD
```

Add repeated `--region`, `--source`, and `--exclude` options when needed. Add `--expected-track-count N` only when the user supplied it.

For an existing run, read `manifest.json`, `checkpoint.json`, and `validation.json` if present. Do not repeat completed work unless its inputs changed or validation failed.

Read [references/research-method.md](references/research-method.md) before planning or searching. Read the other references only when entering their stage:

- Search planning and the discovery gate: [references/search-contract.md](references/search-contract.md)
- Event extraction: [references/event-contract.md](references/event-contract.md)
- Topic synthesis: [references/topic-contract.md](references/topic-contract.md)
- Track synthesis and analysis: [references/track-contract.md](references/track-contract.md)
- Evidence or quality decisions: [references/evidence-and-quality.md](references/evidence-and-quality.md)
- File writing, validation, and delivery: [references/output-contract.md](references/output-contract.md)

## Workflow

1. Establish the scope and write `research-plan.md`, `work-packages.json`, and `search-tasks.jsonl`. Split the research into bounded WorkPackages spanning policy, projects, enterprises, technology commercialization, parks/clusters, investment, and market changes as relevant. Generate the complete search plan before executing any query.
2. Execute every planned SearchTask. For each query, search the web, record its result count and completion state, and link every retained or uncertain URL to that SearchTask in `sources.jsonl`. Do not silently skip, replace, or combine planned queries. Add a new SearchTask record when supplementary searching is needed; do not perform unrecorded searches. A search snippet is discovery metadata, never evidence.
3. Normalize URLs before adding them:

   ```text
   python "<skill-dir>/scripts/normalize_urls.py" <url> [<url> ...]
   ```

4. Batch-acquire every retained or uncertain `content_page` after its Source record exists:

   ```text
   python "<skill-dir>/scripts/fetch_pages.py" <workspace>
   ```

   The fetcher validates URL syntax but does not resolve hostnames to classify private/public addresses, so local proxy and TUN/Fake-IP DNS responses do not block public-site acquisition. It handles ordinary public HTML, textual PDF, local files, retries, and raw PDF preservation. It records `reader_required` for pages whose useful text cannot be obtained statically and `ocr_required` for image-only PDFs. Process every such Page individually with an available browser/web-reader or OCR-capable model, save the obtained text to a temporary UTF-8 file, and import it with:

   ```text
   python "<skill-dir>/scripts/import_page_text.py" <workspace> <source-id> <text-file> --method web_reader
   python "<skill-dir>/scripts/import_page_text.py" <workspace> <source-id> <text-file> --method ocr
   ```

   For OCR, use the preserved file under `pages/attachments/`. If individual browser/OCR processing confirms that a page is inaccessible or insufficient, close its pending state explicitly with `--status inaccessible --reason "..."` or `--status insufficient --reason "..."`; the text-file argument may then be omitted. Never bypass login, payment, CAPTCHA, or access controls.
5. Do not begin Event extraction until every WorkPackage and SearchTask is complete and every retained/uncertain content page has an acquisition attempt. Enforce the gate with:

   ```text
   python "<skill-dir>/scripts/check_research_gate.py" <workspace> --write
   ```

   If it fails, continue the missing searches, URL acquisition, browser reading, or OCR. Do not compensate for insufficient coverage by advancing to Event extraction.
6. Read usable page text and append validated Events to `events.jsonl`. Preserve exact evidence quotations and page references.
7. Check coverage and evidence gaps. Perform targeted supplementary research when a material conclusion lacks independent support, source classes are overly concentrated, Topic boundaries are unclear, or the enterprise/SMB chain is weak. Record supplementary WorkPackages/SearchTasks and pass the research gate again before continuing. Stop repeating searches that add no new evidence; record remaining uncertainty.
8. Synthesize `topics.json`, allowing valid Events to remain unassigned rather than forcing a grouping.
9. Synthesize and analyze `tracks.json`. A Track must describe a recognizable enterprise group and operating activities, not merely a region, policy instrument, named project, single company, or broad technology.
10. Validate the workspace:

   ```text
   python "<skill-dir>/scripts/validate_workspace.py" <workspace> --write
   ```

11. Fix validation errors before formal delivery. Write `report.md`, then build the standalone dashboard:

   ```text
   python "<skill-dir>/scripts/build_dashboard.py" <workspace>
   ```

12. Update manifest counts, set `manifest.status` and `checkpoint.current_stage` to `completed`, and run the validator with `--write` again. If the research gate or final validation fails, restore `manifest.status` to `incomplete`, record the remaining gaps, and do not present the run as complete.

## Control Rules

- The Agent chooses the next research action; schemas and quality gates determine whether an artifact can advance.
- Completing a WorkPackage means executing all of its SearchTasks and attempting acquisition for all retained or uncertain content pages. Finding a few plausible Tracks is not a substitute for completing the plan.
- Use scripts for deterministic mechanics, not for semantic judgments.
- Never infer unknown dates, entities, regions, numbers, or relationships. Use `null` or an empty list.
- Preserve all valid source records and evidence when deduplicating events.
- Keep facts, analysis, and uncertainty distinguishable.
- Downgrade insufficiently supported directions to `watchlist`; do not promote them to satisfy an expected count.
- Update `checkpoint.json` after each material batch so another Agent can resume.
- Keep the result directory self-contained. Do not require PostgreSQL, Docker, Alembic, or a project-specific model API key.

## Delivery

The business-facing deliverables are `report.md` and `dashboard.html`. Return their paths and a compact summary of candidate/watchlist counts, evidence coverage, validation status, and unresolved limitations. Do not include hidden reasoning, abandoned approaches, or debugging logs in the deliverables.
