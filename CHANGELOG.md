# Changelog

## 0.2.0a1 — first developer alpha

Preserve explicitly registered requirements across agent compaction and inspect whether the reconstructed context retains them.

- Persistent SQLite registry with exact source spans, original roles, task scopes, supersession, revocation, event deduplication, and revision checks.
- Audit, pin, and experimental repair modes with explicit resume outcomes and bounded Jev assessments.
- Structured before/after traces, JSON Schemas, hash validation, and session-level corpus manifests.
- `check`, `prepare`, and `trace` CLI commands; version 1 constraint files remain supported.
- Local CSV agent with independent checks of actual reads, report contents, and unchanged source files.
- 77 offline tests, installable wheel/source artifacts, and CI checks for supported Python versions.

In the controlled sample, pinning completed 33/33 scheduled steps compliantly; unprotected compaction completed 0/33 compliantly; repair completed 19/33 with 14 stopped or blocked. These are repeated synthetic trials in one task family. See the [comparison](benchmarks/results/continuation-summary.md).

Use explicit `mode="pin"` for the sample pilot. Audit remains the API/CLI default. Repair, confidence thresholds, and semantic judgments remain experimental. The complete serialized context is limited to 24,000 UTF-8 bytes. Natural compaction traces and other agent models have not yet been evaluated.

Earlier 0.1.0 and 0.2.0 builds were local development artifacts and were not public releases.
