# Frontend standardisation and hardening — 23 October 2026

The frontend now uses one bundled Inter font and shared size, weight and
line-height tokens. The reported History disclosure mismatch is fixed. This
follow-up adds provider, saved-route and numeric-model validation without
changing the serving model or claiming production perfection. All changes
remain local; no deployment, push, credential change or remote schema migration
was performed.

## Frontend

`frontend/src/theme/typography.css` defines the application type scale.
Navigation, account/authentication panels, forms, selectors, tables, route
results, analytics, map labels and endpoint pins use those tokens. Dispatch
identifiers and code intentionally use the shared monospace token. The local
variable Inter WOFF2 includes its SIL Open Font License and source provenance.
Font loading no longer depends on Google Fonts at runtime. Leaflet CSS and its
assets are bundled from the installed dependency instead of an external CDN.
This does not remove the map tile and data providers' network requirements.

The Codex browser confirmed the local font loaded, History body/table text
uses 14px with 21px line height, disclosure rows use 14px/600 with the same
21px line height, and report headings use 16px. Existing 16px footer gaps and
inset dropdown arrows are retained. Both closed and expanded disclosures were
inspected. No horizontal page overflow appeared at the available 775px
operator or 1992px driver viewports. This is not a full mobile or screen-reader
certification.

![Corrected history typography](verification/hardening-typography-history.png)

## Backend, database and ML safeguards

| Boundary | Correction and verification |
| --- | --- |
| External provider JSON | Nominatim and direct TomTom Flow read at most 512 KiB plus a sentinel byte; the isolated optional Google Directions adapter is capped at 2 MiB. A shared decoder rejects duplicate fields, non-finite numbers, overflowed floats and malformed JSON/UTF-8. Existing socket timeouts and labelled fallback paths remain in place. |
| Provider error logs | Geocoder and optional Google adapter logs omit raw URLs, queries and provider error content. TomTom's existing key-safe logging remains intact. Regression tests exercise redaction and fallback. |
| Geocoder cache | Deep-copy on insertion and retrieval prevents one caller from mutating another caller's cached results. |
| Dispatch persistence | Reject mismatched response IDs/codes, a changed code for an existing route, and request/response payloads exceeding 64 KiB/8 MiB UTF-8 budgets before writes. |
| Route/history atomicity | An injected history-table failure rolls back the route insert; a retry succeeds. Twenty concurrent file-backed saves retain unique codes and survive database reopen. |
| Numeric ML artifact | Restored feature importances must be finite numbers between 0 and 1 and sum to 1, or all be zero for a constant forest. Corrupt/unnormalised metadata is rejected. Existing data-only numeric artifacts, chronological evaluation and promotion gates remain in place. |

Read-only checks of all **seven existing local SQLite databases** returned
`quick_check=ok` and zero reported foreign-key violations. These checks do not
establish logical completeness, backup recovery or remote Supabase health.
The 26 local PostgreSQL schema, migration and row-level-policy checks also
passed; this pass did not apply remote migrations.

## Final verification

| Check | Result |
| --- | --- |
| Frontend Vitest | 422 tests across 41 files passed. |
| ESLint, TypeScript and production build | Passed after the final shared map-marker font changes. The built HTML preloads the hashed local font asset. |
| Backend/auth pytest | 471 tests and 412 subtests passed in 123.12 seconds. |
| Standalone backend regression runner | 153/153 checks passed; some coverage overlaps pytest. |
| Local SQL schema/migration/policy checks | 26/26 passed. |
| Repository whitespace check | `git diff --check` passed. |
| Real Codex browser | Existing operator/admin and driver Supabase sessions remained valid. Both console error lists were empty. |

One old map-badge test asserted the literal CSS string `12px`; it was updated
to check the correct displayed duration/distance instead. A missing type cast
in that mock assertion was corrected before the final TypeScript/build pass.
The frontend suite emits Node's experimental localStorage warning; tests
provide their own browser storage and all assertions passed.

The operator created **9JL6N52**, saw it in private route history and restored
it. The separate driver account retrieved that same saved route with matching
distance, departure and ETA. The existing ferry demonstration **3B55FVN** also
survived the backend restart and loaded its summary, map and road/ferry
directions: **24 October 2026, 18:05 WIB**. The driver tab is left on that
assignment. All six analytics panels rendered their appropriate API-backed
content: observed Overview, published Ferry & Ports, modelled Trends,
snapshot Alerts, dated History and clearly labelled Model. Empty observed
alerts are valid; unavailable live terminal operations remain explicit.

No password, OTP or Google consent flow was repeated in this pass. Automated
authentication, email isolation, transport and authorisation tests were rerun.
Earlier real login evidence remains in the local verification report.

Evidence: [browser checks](verification/hardening-browser.json),
[computed typography](verification/hardening-typography.json),
[read-only database checks](verification/hardening-database-checks.json),
[test summary](verification/hardening-summary.json),
[driver map](verification/hardening-driver-map.png).

## Remaining material limits

The serving Batam network forest still uses the labelled synthetic baseline.
The real 48-row Batam survey research backtest and separate Singapore academic
benchmark remain separate; sparse, short-window observed data is not promoted
as validated network forecasting. This pass does not create new historical
traffic observations or resolve the free-data availability constraint.

Saved route/history files remain persistent on this local server and are not
shared across deployments. Simulated forecasts, published schedules and
observed point speeds retain separate labels. The successful local tests are
regression evidence, not a guarantee of perfect operation or an exhaustive
penetration test. See the [full website audit](WEBSITE_AUDIT_2026-10-23.md) for
the broader feature audit, actual training/backtests and data limitations.
