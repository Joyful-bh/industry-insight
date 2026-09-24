# Research Method

## Objective

Identify industry tracks that have an observable SMB enterprise base, reachable policy or market signals, repeatable IT workloads, and a plausible Lenovo SMB coverage path.

## Planning

Turn the user scope into a small set of research questions. Cover only relevant dimensions, while normally checking:

- policy release, funding, application, pilot, and recognition;
- project signing, construction, expansion, delivery, and production;
- enterprise products, orders, cooperation, capacity, and market expansion;
- investment, financing, and acquisition;
- technology commercialization, standards, and industrial collaboration;
- demand, supply, price, capacity, and market changes;
- parks, clusters, and regional specialization.

For each question record its purpose, target signals, target source classes, search queries, and completion criterion in `research-plan.md`.

Before executing searches, materialize these questions and queries in `work-packages.json` and `search-tasks.jsonl` according to [search-contract.md](search-contract.md). Complete the whole plan and pass the discovery gate before Event extraction.

## Source Strategy

Prefer primary and independently accountable sources:

1. government departments and official policy pages;
2. park, cluster, association, and standards bodies;
3. company announcements and official company news;
4. authoritative or specialist industry media;
5. investment institutions and market reports.

Use secondary reporting to discover facts and corroborate primary material. Count syndicated copies as one independent event chain. A domain is not automatically authoritative; assess the publishing entity and the specific page.

Required source classes are contextual rather than a mechanical quota. For broad regional manufacturing research, seek meaningful coverage across government, park, association, company, industry media, and authoritative media. Explain genuinely unavailable classes instead of adding irrelevant sources.

## Search and Selection

- Combine topic, region, time, signal type, product, application, enterprise activity, and source qualifiers.
- Record kept, uncertain, and important rejected results with concise reasons.
- Treat `source_entry` pages as discovery aids; seek the specific `content_page` before extracting Events.
- Respect the requested time range. Older material may provide historical context but must be marked as such and must not inflate current-window statistics.
- Specified sources receive priority inspection but remain subject to relevance, accessibility, and evidence rules.
- Apply excluded directions throughout search, synthesis, and reporting.

## Supplementary Research

Perform targeted supplementary searches when:

- an important claim has only one non-primary source;
- source coverage is dominated by one publisher or repost chain;
- a Topic mixes materially different enterprise activities;
- a proposed Track lacks a clear enterprise archetype or value-chain role;
- “why now” relies on assertion instead of dated signals;
- an SMB IT need cannot be connected to an observable workload.

Stop when additional searches repeat known sources, fail to add material evidence, or cannot resolve the gap within reasonable effort. Record unresolved limitations in Track uncertainties and the report.

## Completion

Research is complete when the workspace passes structural and evidence validation, each candidate Track passes all Track gates, watchlist items are identified honestly, and `report.md` plus `dashboard.html` can be opened without a database or running service.
