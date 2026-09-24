# Topic Contract

A Topic is an industrial observation formed from Events with consistent meaning across products, technologies, applications, enterprise activities, or value-chain roles.

## File Shape

Write `topics.json` as:

```json
{
  "schema_version": "topic-v1",
  "topics": [],
  "unassigned_event_ids": []
}
```

Each Topic contains:

- `topic_id`, `canonical_key`;
- `label`, `definition`, `summary`;
- `aliases`, `keywords`;
- `regions`, `industries`;
- `event_ids`;
- `first_seen_at`, `latest_seen_at`;
- `confidence`;
- `status`: normally `candidate`, or `archived` for an incremental superseded object;
- optional `previous_version_id` during incremental updates.

## Synthesis Rules

- Use only existing, validated Event IDs.
- Group by industrial meaning, not broad keyword overlap.
- Keep differences in enterprise activity, product/service, application, and value-chain role visible.
- Do not name a Topic solely after a region or policy instrument.
- Leave Events unassigned when no coherent Topic exists.
- Prefer matching an existing Topic during incremental updates. Create a new Topic only when its boundary is materially different.
- Merge Topics only when the combined definition still identifies a coherent industrial observation.
- Split a Topic when its Events imply different enterprise groups or operating activities.
- Recompute first/latest dates from member Events with non-null signal dates.

