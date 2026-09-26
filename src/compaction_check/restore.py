"""Deterministic, idempotent restoration of explicitly registered wording."""

from dataclasses import dataclass

from .inputs import InputError
from .messages import Message, resequence, validate_messages
from .registry import Snapshot


class OwnershipError(InputError):
    pass


@dataclass(frozen=True)
class Restoration:
    messages: tuple[Message, ...]
    removed_message_ids: tuple[str, ...]
    restored_constraint_ids: tuple[str, ...]


def strip_owned(messages, snapshot: Snapshot):
    kept, removed = [], []
    history = {(r.id, r.version): r for r in snapshot.records}
    for message in validate_messages(messages):
        if message.source_type != "restored_constraint":
            kept.append(message)
            continue
        record = history.get((message.constraint_id, message.constraint_version))
        if (
            message.registry_id != snapshot.registry_id
            or record is None
            or message.role != record.role
            or message.content != record.text
            or message.id != f"cc:{snapshot.registry_id}:{record.id}"
        ):
            raise OwnershipError(
                "Cannot replace a restored message whose provenance is not verified by this registry."
            )
        removed.append(message.id)
    return resequence(kept), tuple(removed)


def restore(messages, snapshot: Snapshot, *, ids=None) -> Restoration:
    base, removed = strip_owned(messages, snapshot)
    active = {r.id: r for r in snapshot.active}
    selected = set(active) if ids is None else set(ids)
    if selected - set(active):
        raise InputError("Only active constraints in the current task scope can be restored.")
    additions = {role: [] for role in ("system", "developer", "user")}
    for record in snapshot.active:
        if record.id in selected:
            additions[record.role].append(
                Message(
                    f"cc:{snapshot.registry_id}:{record.id}",
                    record.role,
                    record.text,
                    0,
                    "restored_constraint",
                    snapshot.registry_id,
                    record.id,
                    record.version,
                )
            )
    result = list(base)
    # Keep original message order. Insert each authority block after the last original
    # message at that authority, or at its canonical boundary when none exists.
    for role in ("system", "developer", "user"):
        if not additions[role]:
            continue
        if role == "user":
            position = len(result)
        else:
            allowed = {"system"} if role == "system" else {"system", "developer"}
            position = max((i + 1 for i, m in enumerate(result) if m.role in allowed), default=0)
        result[position:position] = additions[role]
    return Restoration(
        resequence(result), removed, tuple(r.id for r in snapshot.active if r.id in selected)
    )
