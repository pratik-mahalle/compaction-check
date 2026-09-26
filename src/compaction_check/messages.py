"""Role-preserving messages at a host application's compaction boundary."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace

from .inputs import InputError

ROLES = {"system", "developer", "user", "assistant", "tool"}
SOURCE_TYPES = {
    "instruction",
    "conversation",
    "summary",
    "tool_result",
    "reference",
    "restored_constraint",
}
AUTHORITIES = {"system", "developer", "user"}


def identifier(value, name="Identifier"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise InputError(
            f"{name} must be 1–128 letters, digits, dots, colons, dashes or underscores."
        )
    return value


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Message:
    id: str
    role: str
    content: str
    sequence: int
    source_type: str = "conversation"
    registry_id: str | None = None
    constraint_id: str | None = None
    constraint_version: int | None = None

    def __post_init__(self):
        identifier(self.id, "Message id")
        if self.role not in ROLES or self.source_type not in SOURCE_TYPES:
            raise InputError("Unknown message role or source type.")
        if not isinstance(self.content, str) or len(self.content.encode()) > 1_000_000:
            raise InputError("Message content must be text of at most 1 MB.")
        if type(self.sequence) is not int or self.sequence < 0:
            raise InputError("Message sequence must be a nonnegative integer.")
        owned = (self.registry_id, self.constraint_id, self.constraint_version)
        if self.source_type == "restored_constraint":
            identifier(self.registry_id, "Registry id")
            identifier(self.constraint_id, "Constraint id")
            if type(self.constraint_version) is not int or self.constraint_version < 1:
                raise InputError("Restored constraints need a positive constraint version.")
        elif any(item is not None for item in owned):
            raise InputError("Only restored messages may carry registry ownership fields.")
        if self.source_type == "instruction" and self.role not in AUTHORITIES:
            raise InputError("Only system, developer and user messages can be direct instructions.")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise InputError("Message must be an object.")
        try:
            return cls(**value)
        except (TypeError, KeyError):
            raise InputError("Message has missing or unknown fields.") from None


def validate_messages(messages):
    if not isinstance(messages, (list, tuple)) or len(messages) > 10_000:
        raise InputError("Messages must be a list with at most 10,000 entries.")
    if not all(isinstance(m, Message) for m in messages):
        raise InputError("Expected Message objects.")
    if len({m.id for m in messages}) != len(messages):
        raise InputError("Message ids must be unique within a snapshot.")
    if any(a.sequence >= b.sequence for a, b in zip(messages, messages[1:])):
        raise InputError("Message sequence must be strictly increasing.")
    return tuple(messages)


def resequence(messages):
    return validate_messages([replace(m, sequence=index) for index, m in enumerate(messages)])


def render_messages(messages):
    # JSON escaping prevents content from creating structural role/source fields.
    return json.dumps(
        [m.to_dict() for m in validate_messages(messages)], ensure_ascii=False, indent=2
    )


def messages_hash(messages):
    return digest([m.to_dict() for m in validate_messages(messages)])
