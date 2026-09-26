# compaction-check

**Developer alpha · v0.2.0a1 · MIT license**

[Releases](https://github.com/pratik-mahalle/compaction-check/releases) · [Alpha feedback](https://github.com/pratik-mahalle/compaction-check/issues/new?template=alpha-feedback.yml) · [Changelog](CHANGELOG.md)

Keep explicit agent requirements outside the summary, restore their current wording after compaction, and use Jev to check the reconstructed context. Includes a Python API, CLI, persistent registry, and a local agent experiment.

**Example:** “Ask before deleting any file” becomes “Ask before deleting files if convenient.” The check flags the weakened requirement and fails CI.

**Alpha scope:** use explicit `mode="pin"` to restore registered constraints and verify them with Jev. Audit remains the default; automatic repair is experimental. APIs and trace formats may change. See the [development plan](PLAN.md) for progress and remaining validation.

This is an early library. Its retention judgment does not guarantee that an agent will follow the rule. The sample agent checks actual file reads separately. Provide reviewed requirements and the complete context the agent receives, including separately retained host instructions.

## Quick start

Python 3.11 or newer on Linux or macOS. The runtime has no third-party dependencies.

Install the versioned wheel from the GitHub prerelease:

```sh
python -m pip install https://github.com/pratik-mahalle/compaction-check/releases/download/v0.2.0a1/compaction_check-0.2.0a1-py3-none-any.whl
compaction-check --version
```

To run the examples and local sample agent, clone the release:

```sh
git clone --branch v0.2.0a1 https://github.com/pratik-mahalle/compaction-check.git
cd compaction-check
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Set `TYPESAFE_API_KEY` in your shell or CI secret store. The CLI reads only that environment variable for authentication; it does not load `.env` files. Use your TypeSafe key from [the TypeSafe console](https://console.typesafe.ai).

```sh
compaction-check check \
  --constraints examples/constraints.json \
  --context examples/compacted-good.md

compaction-check check \
  --constraints examples/constraints.json \
  --context examples/compacted-bad.md \
  --output reports/bad.json
```

The first snapshot retains all three active rules. The second drops the dataset restriction, weakens the deletion rule, and requires the wrong report language. Both use the same constraint file. These commands make real API requests. The inactive English-language requirement is skipped locally.

Without installation, use `PYTHONPATH=src python3 -m compaction_check` in place of `compaction-check`.

Start with `python examples/resume.py` for two compactions and a changed user instruction. The [release notes](docs/releases/v0.2.0a1.md) include the full pilot path. This alpha is distributed through GitHub release assets; the install command above does not depend on a PyPI package listing.

## Constraint file

```json
{
  "version": 1,
  "constraints": [
    {
      "id": "dataset",
      "text": "Use only sales_2025.csv as the data source.",
      "source": "user turn 1"
    },
    {
      "id": "old-language",
      "text": "Write the final report in English.",
      "active": false,
      "source": "replaced by the user at turn 5"
    },
    {
      "id": "language",
      "text": "Write the final report in Spanish.",
      "source": "user turn 5"
    }
  ]
}
```

- `id` and `text` are required. IDs must be unique.
- `active` defaults to `true`. Set it to `false` when a rule has been revoked or superseded. At least one rule must be active.
- `source` is an optional reference for human review. It is included in the report but not sent to Jev.
- Unknown fields, duplicate JSON keys, invalid types, and empty rules are rejected.
- Write one requirement per entry where possible. Automatic extraction and conflict resolution are outside this version. Inactive rules are skipped; their absence from the snapshot is not asserted.

## Results and CI policy

| Status | Meaning |
| --- | --- |
| `preserved` | Equivalent operative requirement, including scope and strength. |
| `missing` | No operative version remains. A task description alone is insufficient. |
| `weakened` | A retained rule is relaxed, narrowed, or loses a condition. |
| `contradicted` | The context gives an incompatible operative instruction. |
| `uncertain` | Ambiguous meaning, unsupported change, or confidence below the threshold. |
| `skipped` | The developer explicitly marked this rule inactive. |

**Exit codes:** `0` = every active rule preserved; `1` = at least one retention failure or uncertain result; `2` = invalid input, incomplete evaluation, API failure, or output error. API failures never become passing reports. If a report path is supplied, an API failure replaces a previous report with an explicit incomplete result.

`--format json` emits a machine-readable report to stdout. `--output path.json` also writes an atomic JSON artifact. Each complete report includes context and constraint hashes, policy version, requested and resolved model IDs, per-call token usage when provided, raw model labels, probabilities, confidence, and selected evidence with original line numbers.

The default `--min-confidence 0.6` is a starting policy, **not a validated accuracy guarantee**. It gates both the status and the selected passage for `preserved`, `weakened`, and `contradicted`. The raw model decision remains visible when the CLI changes the final status to `uncertain`. TypeSafe's confidence is derived from its option distribution; it is not the probability that a test is correct. See [TypeSafe's confidence documentation](https://docs.typesafe.ai/confidence).

Evidence is a model-selected location in the original context, copied verbatim by Python. It is not a generated explanation or proof of correctness. A selected passage may be only one part of the evidence for a rule spread over several lines. Missing rules can have a related passage or no passage at all.

## Python integration

```python
from compaction_check import Constraint, JevClient, check

requirements = [Constraint("dataset", "Use only the supplied dataset.")]
context = "All analysis must use the supplied dataset exclusively."
report = check(requirements, context, JevClient())
assert report["passed"], report["results"]
```

Save context snapshots at your existing compaction boundary, or call `check()` on the reconstructed context before resuming the agent. The reference requirements must come from your trusted configuration or reviewed user instructions. Text being evaluated is not an authority to change that reference.

## Preserve rules at the compaction boundary

Register reviewed instructions as the host receives them. The SQLite event log retains the original message, exact source span, role, scope, and every version. New database files use mode `0600`.

```python
from compaction_check import (
    Budget,
    JevClient,
    Message,
    Registry,
    compact_and_prepare,
)

registry = Registry("reports/session.sqlite", session_id="sales-session")
source = Message("user-1", "user", "Use only approved_a.csv.", 1, "instruction")
registry.register("dataset", source, event_id="register-dataset")


def summarize(messages):
    # Replace this fault fixture with your host's summarizer.
    # Return the COMPLETE reconstructed context, including retained instructions.
    return (Message("summary-1", "assistant", "Continue analyzing sales.", 0, "summary"),)


prepared = compact_and_prepare(
    (source,),
    summarize,
    registry,
    JevClient(),
    mode="pin",
    budget=Budget(max_requests=4, max_seconds=15),
)
if prepared.ready:
    messages = prepared.messages_for(registry)  # Recheck revision immediately before resume.
    # Pass these messages to your agent, preserving their roles and order.
else:
    print(prepared.status, prepared.reason)
```

For a changed instruction, call `supersede` with a later original source message and a new event ID. Use `revoke(id, event_id=..., reason=...)` to remove a rule, or `end_scope(task_id, event_id=...)` to close a task. A rule with no task ID applies to the session; a task rule applies only when the matching `task_id` is passed at the boundary. Supersession preserves authority and scope. Every mutation accepts `expected_revision` for concurrent update detection. Repeating an identical event ID is a no-op, including after later revocation.

```python
registry.supersede(
    "dataset",
    Message("user-2", "user", "Use only approved_b.csv.", 2, "instruction"),
    event_id="change-dataset",
    expected_revision=1,
)
```

The host is responsible for trusted roles and source labels. A tool result cannot register itself as an instruction. Original source sequence numbers must increase across the session; reconstructed message sequences describe the order within each snapshot. Serialize registry updates and agent resume within your host session: a revision check is not a lock around subsequent agent actions.

| Mode | Behavior |
| --- | --- |
| `audit` | Assess the supplied context without changing it. This remains the API/CLI default. |
| `pin` | Replace owned restoration blocks with all applicable active rules, then assess the complete result with Jev. |
| `repair` | Remove previous owned blocks, assess retention, restore missing/weakened rules, then reassess. Contradiction or uncertainty stops the operation. |

Restoration copies the registered wording exactly and preserves its original role. Only verified blocks owned by this registry can be replaced. Revoked and superseded versions are removed from those blocks; unrelated messages are never silently edited. Unknown ownership requires review. A conflicting summary can still prevent the final context from passing.

Outcomes are `ready`, `needs_review`, `stale`, `unavailable`, `too_large`, or `budget_exceeded`. A context with no applicable rules can be ready, with an explicit statement that no retention claim was made. `candidate_messages` can contain a proposed patch even when not ready; resume through `messages_for(registry)`, which rejects non-ready and stale results.

Each result records hashes, registry revision, patch, model assessments, request count, reported input tokens, elapsed time, and added context bytes. Request caps disable hidden retries. Input-token budgets are checked after each provider response, so the last request can overshoot the token limit. A native request uses the remaining timeout; a custom synchronous judge cannot be forcibly interrupted, but a late result cannot authorize resume. The time budget covers assessment/restoration, not the host's summarizer.

To work with saved structured messages:

```sh
compaction-check prepare \
  --registry reports/session.sqlite --messages reports/messages.json \
  --expected-revision 2 --mode pin --output reports/prepared.json

# No API call: validate a trace's structure, lifecycle and hashes.
compaction-check trace examples/traces/good.json
```

`prepare` exits `0` for ready, `1` for review, and `2` for stale, unavailable, budget/size failures or invalid input. The registry is created and updated through the Python API. CLI JSON is a snapshot of the decision; a host using it must verify its revision before resume. The original `check` command remains compatible with version 1 files and also accepts version 2 registry exports. See [data formats](schemas/README.md) and the runnable [two-cycle example](examples/resume.py).

## Local sample agent

The agent uses Jev to choose a dataset tool, reads a real disposable CSV, and writes a JSON total. A deterministic fixture compactor deliberately drops the dataset instruction. Each cycle changes the restriction between two files. An independent Python oracle checks actual reads, output contents, and source-file hashes; neither model receives its expected answer.

```sh
# Three repetitions, two compactions and a changed dataset requirement.
python benchmarks/continuation.py --output reports/continuation

# Longer trajectories, with identical settings for all four conditions.
python benchmarks/continuation.py \
  --cycles 1 3 5 --repetitions 3 --output reports/multicycle
```

These commands use real Jev API credits. Each output directory must be new or empty. Artifacts include complete synthetic traces, registry snapshots, model decisions, delivered messages, tool events, reports, and a corpus manifest. Runs that stop keep their incomplete steps in the results.

### Live continuation result

Three repetitions at each of 1, 2, 3 and 5 cycles produced these results with `jev-1.13.0` and the unchanged `0.6` threshold:

| Condition | Compliant completions / scheduled steps | Wrong-dataset reads | Stopped or blocked steps |
| --- | ---: | ---: | ---: |
| Full context | 33 / 33 | 0 | 0 |
| Unprotected compaction | 0 / 33 | 33 | 0 |
| Pin + Jev assessment | 33 / 33 | 0 | 0 |
| Jev-guided repair | 19 / 33 | 0 | 14 |

Repair reached six low-confidence decisions: six steps stopped for review, blocking eight later steps. Its raw labels were `missing`, but confidence fell below the threshold. Pinning completed all 12 trajectories; repair completed 6. Boundary p95 latency was about 1.42 seconds for pinning and 1.93 seconds for repair, with **one active rule**. No threshold tuning was done between these runs.

**Use explicit `mode="pin"` for the sample integration.** It had better completion and fewer boundary calls in this experiment (33 versus 44). This is a provisional choice for one controlled task family; a production default still needs varied, held-out sessions. The compaction failures are injected, and Jev is both actor and judge. These results do not establish accuracy on natural conversations or other agents.

See the [combined comparison](benchmarks/results/continuation-summary.md), [metrics](benchmarks/results/continuation-summary.json), [two-cycle raw report](benchmarks/results/continuation-jev-1.13.0/report.json), and [longer-run raw report](benchmarks/results/multicycle-jev-1.13.0/report.json). The original raw reports used `attempted_steps` for all scheduled cycles; the combined comparison recomputes separate scheduled, attempted, and blocked counts from the saved steps.

## CI integration

After installing the package in your own workflow:

```yaml
- name: Check compacted context
  env:
    TYPESAFE_API_KEY: ${{ secrets.TYPESAFE_API_KEY }}
  run: |
    compaction-check check \
      --constraints tests/constraints.json \
      --context artifacts/compacted-context.txt \
      --output artifacts/retention.json
```

Generate the snapshot before this step with your own summarization pipeline. Keep a known-good snapshot alongside a regressed one to test prompt or model changes. This package does not run the summarizer.

## Jev integration

- Uses the native `POST https://api.typesafe.ai/v1/systemone` endpoint with bearer authentication.
- Pins `jev-1.13.0` by default; override with `--model`. Reports record the actual model returned.
- Makes one request per active constraint, with independent typed Choice questions for status and evidence. No text-generation model is involved.
- Authentication is sent only to the official endpoint. Redirects are disabled. API error bodies and credentials are not logged.
- Retries HTTP `429` and `529` with bounded backoff (default: two retries), respecting retry delays up to 30 seconds. Longer requested delays stop the check. Ambiguous network failures are not replayed automatically.
- TLS verification remains enabled. If Python's default trust store is empty, the client tries the system bundle at `/etc/ssl/cert.pem`.

Requests send the active requirement and complete supplied snapshot to TypeSafe. Reports contain requirement text and evidence, so store them appropriately. The package does not persist your API key.

API details: [TypeSafe API](https://docs.typesafe.ai/api), [models](https://docs.typesafe.ai/models), and [known model limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13).

## Validation

Offline tests require no key and make no network calls:

```sh
python -m unittest discover -s tests -v
```

The 77 tests cover parsing and transport failures, registry lifecycle and concurrency, authority and scope preservation, revision checks, restoration budgets, CLI behavior, and independent file assertions. The local continuation runner has also been exercised with the live Jev API.

Run the opt-in synthetic semantic benchmark with Jev:

```sh
python benchmarks/run.py --output reports/benchmark.json
```

It contains 24 labelled examples, covering paraphrases, omissions, weakened conditions, changed rules, historical quotations, a malicious evaluator instruction, and an empty snapshot. It reports exact status matches, false passes, false alarms, and uncertainty. It uses real API credits. This is a small smoke benchmark, not evidence of production accuracy; add anonymized examples from your own workflows before choosing a threshold.

### First live result

With `jev-1.13.0` and the default threshold, the first live run matched **23 of 24** expected statuses, with **zero false passes and zero false alarms** on this small synthetic set. There were 23 API calls; the empty-context case was resolved locally. The mismatch was “Use the language we discussed earlier”: labelled `uncertain`, judged `missing`. Both fail CI. No prompt tuning was applied after this run.

The [full benchmark result](benchmarks/results/jev-1.13.0.json) includes the actual decisions, probabilities and source hashes at run time. The [good example](examples/results/good.json) passed all three active requirements; the [bad example](examples/results/bad.json) reported missing, weakened and contradicted respectively. Later code formatting and transport-error handling changes do not change the question text used in that run.

## Current scope

The complete serialized snapshot must fit a 24,000 UTF-8 byte application limit, and state plus the largest question must fit 30,000 bytes. These conservative limits are independent of the provider's token limits. Oversized input is rejected without truncation. The registry supports at most 100 active rules, while practical assessment size and call budgets can be lower. Large contexts and automatic constraint extraction remain future work.

The original `check` input is a flat UTF-8 snapshot. Boundary integrations use structured messages and pass their roles and source types to Jev. Authority and source-span validation are deterministic; semantic precedence is still a model judgment. It can misjudge contradictions, quotations, numeric changes, adversarial content, or rules spread across passages. Stronger restrictions are marked uncertain because the checker tests equivalence. Restoration does not enforce tool permissions or guarantee downstream compliance.

## Research context

The [COMPINT paper](https://arxiv.org/abs/2608.11242) and its [implementation](https://github.com/ZhiqiEliWang/compaction-integrity) study constraint loss during compaction and distinguish retention from compliance. This project provides a small Jev-based workflow for explicit application requirements; it does not reproduce the paper's experiments or claim its reported results.
