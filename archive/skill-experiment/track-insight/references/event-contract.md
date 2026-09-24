# Event Contract

An Event is one independently describable policy or industry fact supported by the text of one acquired page. One page may yield zero or more Events.

## Required Record

Append one JSON object per line to `events.jsonl` with:

- `event_id`: stable workspace-unique ID;
- `source_id`, `page_id`: existing references;
- `event_type`, `event_status`;
- `title`, `summary`;
- `signal_date`: ISO date or `null`;
- `date_precision`: `day`, `month`, `year`, or `unknown`;
- `regions`, `industries`, `industry_objects`;
- `topic_hint`;
- `entities`: objects containing `name` and `type`;
- `chain_roles`;
- `smb_relevance`: `high`, `medium`, `low`, or `unclear`;
- `smb_reason`;
- `confidence`: number from 0 to 1;
- `evidence`: one or more evidence objects;
- `evidence_coverage`: arrays named `subject`, `action`, `date`, and `numbers`, containing zero-based evidence indexes;
- `event_fingerprint`;
- `duplicate_group_id`: stable group ID or `null`.

## Event Types

- `policy_release`
- `application_or_funding`
- `recognition_or_list`
- `project_progress`
- `enterprise_operation`
- `investment_financing`
- `technology_commercialization`
- `market_change`
- `park_or_cluster`
- `other_industry_signal`

## Event Statuses

- `published`
- `supported`
- `planned`
- `in_progress`
- `completed`
- `observed`

## Evidence Object

Each item contains `evidence_id`, `quote`, `source_id`, `page_id`, `content_path`, `start_offset`, `end_offset`, and `evidence_type`. Use `null` offsets only when deterministic positioning is unavailable; the exact quote must still occur in the saved page text.

## Extraction Rules

- Extract only facts directly stated by the page.
- Do not turn editorial forecasts, generic background, or the search snippet into a fact.
- A relevant page may correctly produce zero Events.
- The subject and core action require direct evidence.
- Material dates and numbers in the summary require evidence coverage.
- If the evidence cannot support the core fact, discard the Event rather than merely lowering confidence.
- Use `duplicate_group_id` for the same underlying fact reported by multiple pages; keep every valid source and evidence record.
- Do not infer a signal date from the page publication date unless the page explicitly equates them. Preserve publication date separately in the page record.

