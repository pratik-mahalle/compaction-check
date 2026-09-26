# Plan: reduce constraint loss during agent compaction

## Goal

Extend `compaction-check` so a developer can preserve current user requirements across compaction and measure whether their agent follows them afterward.

The first proof should be a repeatable experiment on one agent workflow. Success means fewer observable constraint violations while the agent still completes its task.

## Starting point

At plan creation, the package had a Python API, CLI, Jev judgments, passage references, confidence thresholds, JSON reports, and CI exit codes. It accepted explicit constraints and a flat context snapshot, skipping constraints marked inactive.

Validation so far: 37 offline tests and a live synthetic benchmark with 23/24 expected statuses, zero false passes, and zero false alarms in that sample. This does not establish performance on real conversations. The known mismatch is an ambiguous language reference classified as missing.

Gaps at plan creation:

- No structured capture of messages before and after compaction.
- No versioned record of which constraints are currently active.
- No restoration step or integration with a running agent.
- No tests of the agent's subsequent behavior.
- A 24,000-byte snapshot limit and no formal handling of message authority.

## Implementation progress — September 26, 2026

The user selected a **local sample agent** for the first integration. Version 0.2 implements the first batch: structured trace import/validation and schemas, persistent SQLite lifecycle events, role-preserving restoration, bounded Jev assessments, CLI preparation, and the local CSV agent. There are 77 passing offline tests.

Live experiments used three repetitions with 1, 2, 3 and 5 compaction cycles. Each cycle changed the dataset restriction; the fixture compactor deliberately dropped it. Pinning completed all 33 scheduled steps compliantly, matching full context. Unprotected compaction completed all 33 with wrong-dataset reads. Repair completed 19 compliantly; six low-confidence reviews blocked another eight steps. The unchanged 0.6 threshold remained fixed throughout.

The [comparison](benchmarks/results/continuation-summary.md) and [source-linked metrics](benchmarks/results/continuation-summary.json) use actual Jev decisions and independent file assertions. These are synthetic development fixtures in one task family, with no observed production sessions or held-out evaluation. Use pinning provisionally in this sample. The default library mode remains audit until the broader evaluation is complete.

Remaining work: collect natural compaction traces, add other constraint families and host models, evaluate scoped exceptions, freeze a varied corpus before a held-out run, and decide the production default. The 24,000-byte limit remains. Phase 5's accuracy and five-rule latency gates have not been established.

## Decisions for this iteration

1. Keep Python and the existing CLI. Add a small integration API around them.
2. Use Jev for bounded semantic judgments: retention, ambiguity, and candidate relevance. Keep versioning, authority, repair limits, and resume decisions in code.
3. Start with constraints explicitly registered by the host application. Add automatic extraction after the preservation experiment works.
4. Reuse the host agent's existing model and summarizer. Jev supplies typed decisions; it does not generate summaries or rewritten instructions.
5. Compare Jev-guided restoration with a simple baseline that always reattaches active constraints. Let the results determine the default mode.

## Work sequence

Effort estimates assume one developer and access to suitable traces. Phases 1–5 should take roughly 8–12 working days; phase 6 is a later extension. These are planning estimates, not delivery commitments.

### 1. Capture real compaction examples — 1–2 days

**Question:** What does constraint loss look like in an actual workflow?

- [x] Choose one Python agent that exposes a summarization callback and can run with test tools: the local Jev CSV agent.
- [x] Define a trace format containing message IDs, roles, source types, chronological order, the compactor input, the reconstructed output context, and the active constraint version.
- [x] Record the complete context delivered to the agent, including separately retained system/developer instructions and recent messages.
- [ ] Import an initial target of 20 distinct sessions and at least 50 compaction boundaries. Keep real observations and deliberately altered examples labelled separately.
- [ ] Label active constraints and retention outcomes manually. Include paraphrases, changes of mind, conditional instructions, quotations, conflicting rules, and multiple compaction cycles.
- [ ] Split by session or task family before tuning. Keep approximately one third untouched for final evaluation. Variants of the same conversation belong in the same split.

**Deliverables:** `traces.py`, a versioned trace schema, an importer, and a corpus manifest. Private traces stay outside the repository; check in only suitable anonymized fixtures.

**Done when:** We can replay saved boundaries, locate the source of every reference constraint, and distinguish real observed losses from injected failures. If enough real traces are unavailable, continue with labelled fixtures and retain that limitation in the results.

### 2. Track active constraints outside the summary — 1–2 days

**Question:** Which rules should still apply when the agent resumes?

- [x] Add a persistent registry with `register`, `supersede`, `revoke`, and `end_scope` operations.
- [x] Store a stable ID, original text, source message/span, original authority, session/task scope, revision, and active state.
- [x] Make updates explicit and append a history entry so the active set can be reconstructed at any checkpoint.
- [x] Add a version 2 input format while continuing to accept version 1 files.
- [x] Persist a registry revision before compaction and verify that the revision is still current before resuming.
- [ ] Test restarts, duplicate updates, task boundaries, scoped exceptions, and supersession chains.

Only trusted application events and reviewed user instructions may update the registry. Retrieved documents, tool results, and quoted text cannot grant themselves instruction authority. User constraints retain their original authority when reconstructed.

**Deliverables:** `registry.py`, schema migration, and lifecycle fixtures.

**Done when:** Revoked rules stay revoked across restarts and repeated compaction. An update received during evaluation invalidates the stale evaluation before resume.

### 3. Add restoration at the compaction boundary — 2–3 days

**Question:** Can we put the correct instructions back without changing their meaning?

Build three modes:

| Mode | Behavior |
| --- | --- |
| Audit | Run the retention check and record findings without changing the supplied context. |
| Pin | Reconstruct a dedicated block of all active constraints from the registry on every compaction. |
| Repair | Use Jev to identify loss and restore eligible active constraints from the registry. |

Proposed integration flow:

```text
Capture messages + registry revision
                  ↓
Run host's summarizer
                  ↓
Assemble the actual context the agent will receive
                  ↓
Audit / pin / repair
                  ↓
Verify registry revision, context budget, and final retention
                  ↓
Return prepared messages and an explicit resume result
```

- [x] Restore original registered wording using deterministic code.
- [x] Preserve message roles and source labels; do not promote user instructions into system instructions.
- [x] Make restoration idempotent: replace the owned constraint block rather than appending duplicates on each cycle.
- [x] For a clear omission or weakening, insert the current authoritative wording and check the complete rebuilt context.
- [x] For contradiction, ambiguity, or unknown instruction precedence, return `needs_review`. Adding a rule beneath an opposing rule is insufficient.
- [x] Return `unavailable` for API failures and `too_large` when the complete check cannot fit. Neither means the context passed.
- [x] Bound work to one initial assessment, one repair, and one final assessment. Expose request, latency, and token budgets. Input-token limits are checked after each response, not guaranteed spending caps.
- [x] Record the original context hash, registry revision, patch, final hash, judgments, and final resume result.

**Deliverables:** `restore.py`, `boundary.py`, `audit`, `pin`, and `repair` examples, plus a proposed `prepare_resume(...)` library entry point. Keep the current `check` command compatible.

**Done when:** Each returned `ready` result has a current registry revision and a successful final assessment. A second restoration makes no further changes. Contradictory and unavailable cases have explicit outcomes.

### 4. Test the agent's actual behavior — 2–3 days

**Question:** Does preservation reduce violations after the agent resumes?

Create a controlled continuation runner using the selected host agent and tools backed by local test data. Start with tasks whose outcomes can be checked directly:

| Constraint | Observable check |
| --- | --- |
| Use only the designated dataset | Inspect which datasets the agent's tools read. |
| Obtain approval before deleting a file | Check whether a delete call occurs before an approval event. |
| Draft messages only | Check that no send operation occurs. |
| Leave source files unchanged | Compare file hashes and write traces. |
| Produce a required output structure | Validate the generated artifact against its schema. |
| Apply a changed or revoked instruction | Check actions against the active registry revision. |

Compare four conditions on the same tasks:

1. Full original context, where it fits the host model: a reference for existing instruction-following failures.
2. Compaction with no protection.
3. Compaction with all active constraints pinned.
4. Compaction with Jev-guided repair.

Keep the host model version, summarizer, tools, task budget, and initial fixture state consistent. Reset tool state for each branch. Use three repetitions initially and test tasks with one, three, and five compaction cycles. Store incomplete runs and review stops alongside successful runs.

Use tool traces, state changes, and artifact checks as the main behavioral evidence. Use human review for outcomes without a direct assertion. Jev's own retention verdict must not be the sole judge of whether its repair succeeded.

**Deliverables:** `benchmarks/continuation.py`, scenario fixtures, independent assertions, and a comparison report.

**Done when:** One command reproduces all conditions, reports both task completion and violations, and exposes examples where a retained rule was still ignored. Report results for all attempted tasks and for completed tasks separately so stopping every run cannot appear to solve the problem.

### 5. Evaluate on held-out sessions and choose a default — 2 days

**Question:** Which mode gives developers a useful improvement at acceptable cost?

Freeze prompts, registry rules, thresholds, and fixture labels before the held-out run. Inspect errors by constraint type, context length, and number of compactions. Keep model/API failures separate from semantic errors.

Track:

- Missed losses: known lost constraints incorrectly reported as preserved.
- False alarms: genuinely preserved constraints flagged as failures or uncertain.
- Uncertainty and coverage: how often the system can make a usable decision.
- Behavioral violation rate and successful task completion rate.
- Revoked or superseded constraints incorrectly restored.
- API failures, resume stops, model calls, added context tokens, and median/p95 latency.

**Proposed pilot gates** — targets to test, not current performance claims:

| Measure | Initial gate |
| --- | --- |
| Detection on held-out labelled losses | At most 5% missed losses; report counts and confidence intervals. |
| Preserved constraints | At most 10% false alarms, with uncertainty reported separately. |
| Registry correctness | Zero stale-rule restorations in the lifecycle test suite. |
| Subsequent behavior | At least 40% relative reduction in violations versus unprotected compaction. |
| Task completion | No more than a five percentage point drop versus unprotected compaction. |
| Runtime overhead | Initial target: p95 added latency under two seconds for five active rules within the current snapshot limit. |

If the unprotected baseline has too few violations, expand the experiment; a percentage reduction would be misleading. Small samples justify a limited pilot, not a general reliability claim. Record the denominators, paired comparisons, and uncertainty around the measurements.

**Decision:** If always pinning performs as well as repair at lower cost, make pinning the default and use Jev for auditing. Choose conditional repair only when measured quality or context savings justify it. If neither improves behavior enough, keep the product focused on diagnostics and publish the failure analysis.

**Deliverables:** a versioned evaluation report, the selected default mode, a working integration example, and documentation of supported cases and measured limits.

### 6. Reduce manual setup after the core experiment

Run these as separate follow-up projects:

- Extract candidate constraints from user messages. Preserve exact source spans and let explicit host events determine activation and revocation. Jev can select or classify candidates; generation requires a separate component.
- Support large contexts with full coverage. Retrieval or chunking must preserve conflicting and related passages; absence from a retrieved subset cannot establish that a rule is missing.
- Add one framework adapter based on the first pilot's actual integration needs.
- Add a larger evaluation corpus and tune confidence thresholds on development data for each pinned model version.

## Inputs needed for the next pilot

- One production agent workflow and access to its compaction boundary; the local sample is complete.
- Suitable recorded traces, or permission to collect new traces for that workflow.
- The host agent's existing model/summarizer configuration and an agreed evaluation budget.

The local Jev agent and retention integration have been exercised live. The next pilot needs natural traces and other task families. No credentials belong in this plan or its fixtures.

## First implementation batch

1. [x] Add the structured trace schema and convert the existing good/bad examples into trace fixtures.
2. [x] Add a small versioned constraint registry with explicit supersede and revoke operations.
3. [x] Implement idempotent pinning with original authority and scope preserved.
4. [x] Connect the current Jev checker to the fully reconstructed context.
5. [x] Demonstrate two compactions and a user changing one requirement between them.
6. [x] Add one continuation task with tool assertions and compare full context, unprotected, pin, and repair modes.

This batch should produce the first evidence that the package can preserve the correct rules through a real agent transition. Expand only after the continuation test exposes the remaining failure modes.

## References

- [Current package behavior and validation](README.md).
- [Existing synthetic benchmark](benchmarks/results/jev-1.13.0.json).
- [COMPINT research](https://arxiv.org/abs/2608.11242) and [implementation](https://github.com/ZhiqiEliWang/compaction-integrity): retention and compliance are separate measurements.
- [TypeSafe primitives](https://docs.typesafe.ai/primitives) and [confidence](https://docs.typesafe.ai/confidence): Jev supplies structured decisions; the application chooses how to act on them.
