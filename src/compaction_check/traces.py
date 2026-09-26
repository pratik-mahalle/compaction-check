"""Import and export complete, labelled compaction boundary snapshots."""

from dataclasses import dataclass
from pathlib import Path

from .inputs import InputError, read_text, strict_json
from .messages import Message, identifier, messages_hash, validate_messages
from .registry import Snapshot, snapshot_from_dict


@dataclass(frozen=True)
class Trace:
    boundary_id: str
    origin: str
    before: tuple[Message, ...]
    after: tuple[Message, ...]
    registry: Snapshot
    expected: dict[str, str]

    def __post_init__(self):
        identifier(self.boundary_id, "Boundary id")
        if not isinstance(self.origin, str) or self.origin not in {"synthetic", "observed"}:
            raise InputError("Trace origin must explicitly be synthetic or observed.")
        validate_messages(self.before)
        validate_messages(self.after)
        if not isinstance(self.registry, Snapshot):
            raise InputError("Trace requires a registry snapshot.")
        if not isinstance(self.expected, dict) or set(self.expected) - {
            r.id for r in self.registry.active
        }:
            raise InputError("Expected labels may name only active constraints.")
        if any(
            not isinstance(value, str)
            or value not in {"preserved", "missing", "weakened", "contradicted", "uncertain"}
            for value in self.expected.values()
        ):
            raise InputError("Unknown expected retention label.")
        # Sources can predate the current compactor input; the registry retains exact original spans.
        sources = {m.id: m for m in self.before}
        for record in self.registry.records:
            if record.source.id in sources:
                original, current = record.source, sources[record.source.id]
                if (original.role, original.content, original.source_type) != (
                    current.role,
                    current.content,
                    current.source_type,
                ):
                    raise InputError("Registry provenance disagrees with a message in the trace.")

    def to_dict(self):
        return {
            "version": 1,
            "kind": "compaction_trace",
            "boundary_id": self.boundary_id,
            "origin": self.origin,
            "before": [m.to_dict() for m in self.before],
            "after": [m.to_dict() for m in self.after],
            "registry": self.registry.to_dict(),
            "expected": self.expected,
            "before_sha256": messages_hash(self.before),
            "after_sha256": messages_hash(self.after),
        }

    @classmethod
    def from_dict(cls, data):
        fields = {
            "version",
            "kind",
            "boundary_id",
            "origin",
            "before",
            "after",
            "registry",
            "expected",
            "before_sha256",
            "after_sha256",
        }
        if (
            not isinstance(data, dict)
            or set(data) != fields
            or type(data["version"]) is not int
            or data["version"] != 1
            or data["kind"] != "compaction_trace"
        ):
            raise InputError("Expected a version 1 compaction trace.")
        if not isinstance(data["before"], list) or not isinstance(data["after"], list):
            raise InputError("Trace message snapshots must be arrays.")
        trace = cls(
            data["boundary_id"],
            data["origin"],
            tuple(Message.from_dict(m) for m in data["before"]),
            tuple(Message.from_dict(m) for m in data["after"]),
            snapshot_from_dict(data["registry"]),
            data["expected"],
        )
        if data["before_sha256"] != messages_hash(trace.before) or data[
            "after_sha256"
        ] != messages_hash(trace.after):
            raise InputError("Trace content does not match its recorded hashes.")
        return trace


def load_trace(path):
    return Trace.from_dict(strict_json(read_text(Path(path), 10_000_000)))


def corpus_manifest(traces, *, held_out_sessions=()):
    """Assign whole sessions, including their variants, to one split."""
    held_out = set(held_out_sessions)
    known = {t.registry.session_id for t in traces}
    if held_out - known:
        raise InputError("Held-out sessions must exist in the corpus.")
    ids = [(t.registry.session_id, t.boundary_id) for t in traces]
    if len(ids) != len(set(ids)):
        raise InputError("Duplicate boundary within a corpus session.")
    return {
        "version": 1,
        "sessions": len(known),
        "boundaries": len(traces),
        "observed_boundaries": sum(t.origin == "observed" for t in traces),
        "entries": [
            {
                "session_id": t.registry.session_id,
                "boundary_id": t.boundary_id,
                "origin": t.origin,
                "split": "held_out" if t.registry.session_id in held_out else "development",
                "before_sha256": messages_hash(t.before),
                "after_sha256": messages_hash(t.after),
            }
            for t in traces
        ],
    }
