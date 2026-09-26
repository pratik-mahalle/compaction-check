"""Prepare an agent's next context with explicit resume outcomes and budgets."""

import math
import time
from dataclasses import dataclass, field, replace

from .checker import check
from .inputs import MAX_CONTEXT_BYTES, InputError
from .jev import JevClient, ProviderError, unit_number
from .messages import Message, messages_hash, render_messages, validate_messages
from .registry import Registry, RegistryConflict
from .restore import OwnershipError, restore, strip_owned


class BudgetExceeded(ProviderError):
    pass


@dataclass(frozen=True)
class Budget:
    max_requests: int = 20
    max_seconds: float = 30
    max_input_tokens: int | None = None

    def __post_init__(self):
        if type(self.max_requests) is not int or not 0 <= self.max_requests <= 200:
            raise InputError("Request budget must be an integer from 0 to 200.")
        if (
            type(self.max_seconds) not in (int, float)
            or not math.isfinite(self.max_seconds)
            or not 0 < self.max_seconds <= 300
        ):
            raise InputError("Time budget must be greater than zero and at most 300 seconds.")
        if self.max_input_tokens is not None and (
            type(self.max_input_tokens) is not int or self.max_input_tokens < 1
        ):
            raise InputError("Input-token budget must be a positive integer.")


class _BudgetJudge:
    def __init__(self, judge, registry, revision, budget, started):
        self.judge, self.registry, self.revision = judge, registry, revision
        self.model = judge.model
        self.budget, self.started = budget, started
        self.requests = self.input_tokens = 0
        self.unknown_usage_calls = 0

    def evaluate(self, state, questions):
        self.registry.assert_revision(self.revision)
        remaining = self.budget.max_seconds - (time.monotonic() - self.started)
        if remaining <= 0 or self.requests >= self.budget.max_requests:
            raise BudgetExceeded("The compaction assessment exhausted its request or time budget.")
        judge = self.judge
        if isinstance(judge, JevClient):
            # Hidden retries would defeat a strict request budget. Return errors to the host.
            judge = replace(judge, retries=0, timeout=min(judge.timeout, remaining))
        self.requests += 1
        decision = judge.evaluate(state, questions)
        tokens = decision.usage.get("input_tokens")
        if tokens is None:
            self.unknown_usage_calls += 1
            if self.budget.max_input_tokens is not None:
                raise BudgetExceeded(
                    "Provider usage is missing; the input-token budget cannot be checked."
                )
        else:
            self.input_tokens += tokens
        self.registry.assert_revision(self.revision)
        if time.monotonic() - self.started > self.budget.max_seconds:
            raise BudgetExceeded("The compaction assessment exceeded its time budget.")
        if (
            self.budget.max_input_tokens is not None
            and self.input_tokens > self.budget.max_input_tokens
        ):
            raise BudgetExceeded(
                "Reported input-token usage exceeded the budget; no more calls were made."
            )
        return decision


@dataclass(frozen=True)
class PreparedResume:
    status: str
    reason: str
    mode: str
    registry_id: str
    registry_revision: int
    registry_sha256: str
    original_sha256: str
    candidate_messages: tuple[Message, ...]
    assessments: tuple[dict, ...] = ()
    patch: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)

    @property
    def ready(self):
        return self.status == "ready"

    def messages_for(self, registry: Registry):
        """Call immediately before resuming. A result authorizes only its registry revision."""
        if not self.ready:
            raise RegistryConflict(f"Context is not ready to resume: {self.status}.")
        if registry.registry_id != self.registry_id:
            raise RegistryConflict("Resume result belongs to a different registry.")
        registry.assert_revision(self.registry_revision)
        return self.candidate_messages

    def to_dict(self):
        return {
            "schema_version": 1,
            "kind": "prepared_resume",
            "status": self.status,
            "ready": self.ready,
            "reason": self.reason,
            "mode": self.mode,
            "registry_id": self.registry_id,
            "registry_revision": self.registry_revision,
            "registry_sha256": self.registry_sha256,
            "original_sha256": self.original_sha256,
            "final_sha256": messages_hash(self.candidate_messages),
            "candidate_messages": [m.to_dict() for m in self.candidate_messages],
            "assessments": list(self.assessments),
            "patch": self.patch,
            "metrics": self.metrics,
        }


def prepare_resume(
    messages,
    registry: Registry,
    judge=None,
    *,
    task_id=None,
    mode="audit",
    expected_revision=None,
    budget=None,
    min_confidence=0.6,
) -> PreparedResume:
    if mode not in {"audit", "pin", "repair"}:
        raise InputError("Mode must be audit, pin or repair.")
    if not unit_number(min_confidence):
        raise InputError("Minimum confidence must be between zero and one.")
    if expected_revision is not None and (
        type(expected_revision) is not int or expected_revision < 0
    ):
        raise InputError("Expected revision must be a nonnegative integer.")
    original = validate_messages(messages)
    snapshot = registry.snapshot(task_id)
    if task_id in snapshot.closed_tasks:
        raise InputError("Cannot resume a completed task scope.")
    started = time.monotonic()
    budget = budget or Budget()
    if not isinstance(budget, Budget):
        raise InputError("Provide a Budget object.")
    wrapped = _BudgetJudge(judge, registry, snapshot.revision, budget, started) if judge else None
    candidate, assessments, patch = original, [], {}

    def finish(status, reason):
        # Check again even after errors so callers never mistake stale evidence for current evidence.
        if registry.revision != snapshot.revision:
            status, reason = (
                "stale",
                "Registry changed during compaction; capture the current revision and retry.",
            )
        return PreparedResume(
            status,
            reason,
            mode,
            snapshot.registry_id,
            snapshot.revision,
            snapshot.sha256,
            messages_hash(original),
            candidate,
            tuple(assessments),
            patch,
            {
                "requests": wrapped.requests if wrapped else 0,
                "reported_input_tokens": wrapped.input_tokens if wrapped else 0,
                "unknown_usage_calls": wrapped.unknown_usage_calls if wrapped else 0,
                "elapsed_seconds": time.monotonic() - started,
                "added_context_bytes": len(render_messages(candidate).encode())
                - len(render_messages(original).encode()),
            },
        )

    def assess(items):
        if wrapped is None:
            raise ProviderError("A Jev judge is required to assess active constraints.")
        report = check(
            snapshot.constraints(),
            render_messages(items),
            wrapped,
            min_confidence=min_confidence,
            reference_roles={r.id: r.role for r in snapshot.active},
        )
        assessments.append(report)
        return report

    try:
        if expected_revision is not None and expected_revision != snapshot.revision:
            return finish("stale", "Registry changed while the host was compacting context.")
        if len(render_messages(original).encode()) > MAX_CONTEXT_BYTES:
            return finish(
                "too_large", "Complete context exceeds the assessment limit; nothing was truncated."
            )
        base, removed = strip_owned(original, snapshot)
        if mode == "audit":
            # Audit makes no mutations. A stale owned block is still evaluated as supplied.
            owned_stale = any(
                m.source_type == "restored_constraint"
                and not any(
                    r.id == m.constraint_id and r.version == m.constraint_version
                    for r in snapshot.active
                )
                for m in original
            )
            if owned_stale:
                return finish(
                    "needs_review", "The context contains a restored rule that is no longer active."
                )
            if not snapshot.active:
                return finish(
                    "ready",
                    "No registered constraints apply to this task; no retention claim was made.",
                )
            report = assess(original)
            return finish(
                "ready" if report["passed"] else "needs_review",
                "Audit completed without modifying context.",
            )

        candidate = base
        patch = {"removed_message_ids": list(removed), "restored_constraint_ids": []}
        if not snapshot.active:
            return finish(
                "ready",
                "Removed owned inactive rules; no registered constraints apply to this task.",
            )
        if mode == "repair":
            initial = assess(base)
            if any(r["status"] in {"uncertain", "contradicted"} for r in initial["results"]):
                return finish(
                    "needs_review",
                    "An ambiguous or contradictory rule requires review before restoration.",
                )
            ids = {r["id"] for r in initial["results"] if r["status"] in {"missing", "weakened"}}
            if not ids:
                return finish(
                    "ready", "All current requirements survived; no restoration was needed."
                )
        else:
            ids = {r.id for r in snapshot.active}
        restoration = restore(base, snapshot, ids=ids)
        candidate = restoration.messages
        patch["restored_constraint_ids"] = list(restoration.restored_constraint_ids)
        final = assess(candidate)
        return finish(
            "ready" if final["passed"] else "needs_review",
            "Prepared context passed the final assessment."
            if final["passed"]
            else "The rebuilt context still has unresolved constraint failures.",
        )
    except OwnershipError as exc:
        return finish("needs_review", str(exc))
    except RegistryConflict as exc:
        return finish("stale", str(exc))
    except BudgetExceeded as exc:
        return finish("budget_exceeded", str(exc))
    except ProviderError as exc:
        return finish("unavailable", str(exc))
    except InputError as exc:
        # Context size failures from the checker are distinct from model judgments.
        if "budget" in str(exc) or "exceeds" in str(exc):
            return finish("too_large", str(exc))
        raise


def compact_and_prepare(
    messages, summarize, registry, judge=None, *, task_id=None, mode="audit", **kwargs
):
    """Host callback receives structured messages and returns the COMPLETE reconstructed context."""
    before = validate_messages(messages)
    revision = registry.revision
    after = validate_messages(summarize(before))
    return prepare_resume(
        after, registry, judge, task_id=task_id, mode=mode, expected_revision=revision, **kwargs
    )
