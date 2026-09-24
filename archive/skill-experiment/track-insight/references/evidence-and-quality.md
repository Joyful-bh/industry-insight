# Evidence and Quality

## Evidence Invariants

- Formal evidence comes from saved page text or an explicitly supplied local document.
- Search snippets, model knowledge, and unattributed summaries are discovery aids only.
- Every Event has at least one exact quote that occurs in its referenced content file.
- Evidence references an existing Source and Page, and those IDs agree with the Event.
- Subjects and core actions have evidence coverage.
- Material dates and numbers used in the Event summary have evidence coverage.
- Final Track conclusions cite validated Event IDs; every citation must resolve through Page to Source.

## Source and Page Quality

- Preserve redirected final URLs and publication dates only when observed.
- Do not classify login, CAPTCHA, empty shell, error, or navigation-only pages as usable.
- Distinguish acquisition failure from irrelevance.
- Never bypass authentication, payment, robots enforcement, CAPTCHA, or other access controls.
- Treat a user-specified source as prioritized, not prevalidated.

## Coverage Checks

Assess source diversity by publishing entity first and registrable domain second. Detect syndication and repeated coverage of one underlying event. For broad research, inspect whether government, park/cluster, association, company, industry media, and authoritative media perspectives are meaningfully represented.

Do not add irrelevant sources merely to satisfy a category count. Record a coverage warning when a useful class is unavailable or one source dominates.

## Advancement Gates

- No usable page text: no Event.
- Invalid or unsupported Event: no Topic membership.
- Incoherent Topic: no core Track membership.
- Unclear enterprise group or SMB causal chain: Track remains `watchlist` or is omitted.
- Broken references or evidence mismatch: delivery validation fails.

## Facts and Analysis

Keep these distinct:

- Fact: directly supported Event statement.
- Synthesis: grouping or interpretation across facts.
- Business analysis: reasoned implications for workloads, IT needs, and coverage.
- Uncertainty: missing history, weak source diversity, ambiguous boundaries, or unverified assumptions.

## Stop Conditions

Supplementary research stops when additional searches repeatedly return known information, no accessible primary evidence can be found, or the unresolved point is not material to the Track decision. Preserve the limitation instead of fabricating certainty.

