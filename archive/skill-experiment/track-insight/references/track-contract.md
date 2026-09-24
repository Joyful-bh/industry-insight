# Track Contract

A Track is a recognizable enterprise group sharing operating activities, observable features, relatively stable value-chain roles, and similar repeatable IT workloads. It must be useful for Lenovo SMB identification and coverage.

## Required Record

Write `tracks.json` as an object containing `schema_version` and `tracks`. Each Track contains:

- `track_id`, `canonical_key`, `name`, `aliases`, `definition`;
- `topic_memberships`: objects with `topic_id`, `role`, `relevance_score`, and `reason`;
- `enterprise_archetype`;
- `core_products_services`;
- `core_company_types`, `supporting_company_types`;
- `shared_demand_drivers`;
- `included_activities`, `excluded_activities`;
- `chain_roles`;
- `observable_company_features`;
- `possible_it_needs`;
- `supporting_event_ids`, `regions`;
- `granularity_assessment`;
- `analysis`;
- `confidence`;
- `status`: `candidate` or `watchlist`;
- optional `previous_version_id` during incremental updates.

Topic membership roles are `core`, `supporting`, and `adjacent`.

## Track Gates

A candidate Track must pass all four gates:

1. Signal: valid Events and sources establish a meaningful pattern rather than a single unsupported assertion or repost chain.
2. Boundary: inclusion, exclusion, and adjacent directions are explicit.
3. Enterprise group: participating company types, operating activities, and value-chain roles are describable and observable.
4. Business value: repeatable workloads, IT needs, and a plausible Lenovo SMB coverage path can be explained.

Use `watchlist` when the direction is relevant but one or more gates lack sufficient support. Expected Track count never overrides these gates.

Do not define a Track as only a target region, policy instrument, single project, single enterprise, or broad technology label. Normalize away region wording when region is merely the research scope rather than the enterprise identity.

## Analysis

The `analysis` object contains:

- `summary`, `why_now`;
- `signal_statistics`;
- `activity_assessment` with `label`, `historical_comparability`, and `basis`;
- `industry_chain_analysis`;
- `smb_value_analysis`;
- `evidence_event_ids`;
- `uncertainties`.

Activity labels are `newly_observed`, `recently_active`, `continuously_active`, and `insufficient_history`.

Each SMB value item must form this explicit chain:

```text
business_activity → workload → it_need → coverage_path
```

Do not jump directly from an industry name to a generic product list. Derive the workload from what the enterprise actually builds, operates, processes, stores, connects, secures, or collaborates on.

