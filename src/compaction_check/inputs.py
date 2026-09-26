"""Strict input validation and verbatim, line-addressable evidence."""

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

# These are conservative application limits, not the provider's token limits.
MAX_CONTEXT_BYTES = 24_000
MAX_CONSTRAINTS = 100
MAX_PASSAGES = 128


class InputError(ValueError):
    pass


@dataclass(frozen=True)
class Constraint:
    id: str
    text: str
    active: bool = True
    source: str | None = None


@dataclass(frozen=True)
class Passage:
    id: str
    start_line: int
    end_line: int
    text: str

    def to_dict(self):
        return asdict(self)


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("Duplicate JSON object keys are not allowed.")
        result[key] = value
    return result


def _invalid_constant(_):
    raise InputError("JSON must not contain NaN or Infinity.")


def strict_json(text):
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_invalid_constant)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError("Input is not valid JSON.") from None


def parse_constraints(data) -> list[Constraint]:
    if isinstance(data, dict) and type(data.get("version")) is int and data["version"] == 2:
        from .registry import snapshot_from_dict

        snapshot = snapshot_from_dict(data)
        # Historical/revoked records must not consume the active-check limit.
        latest = {record.id: record for record in snapshot.active}
        data = {
            "version": 1,
            "constraints": [
                {
                    "id": r.id,
                    "text": r.text,
                    "active": r.applies(snapshot.task_id),
                    "source": f"{r.source.id}:{r.span_start}-{r.span_end}",
                }
                for r in latest.values()
            ],
        }
    if not isinstance(data, dict) or set(data) != {"version", "constraints"}:
        raise InputError("Constraint file must contain exactly 'version' and 'constraints'.")
    if type(data["version"]) is not int or data["version"] != 1:
        raise InputError("Constraint file version must be 1.")
    items = data["constraints"]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_CONSTRAINTS:
        raise InputError(f"Provide between 1 and {MAX_CONSTRAINTS} constraints.")
    result, ids = [], set()
    for index, item in enumerate(items, start=1):
        prefix = f"Constraint {index}"
        if not isinstance(item, dict) or set(item) - {"id", "text", "active", "source"}:
            raise InputError(
                f"{prefix} must be an object with id, text, optional active and source."
            )
        name, text = item.get("id"), item.get("text")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise InputError(
                f"{prefix} needs a unique id of 1–64 letters, digits, dots, dashes or underscores."
            )
        if name in ids:
            raise InputError(f"Duplicate constraint id: {name}.")
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 2_000:
            raise InputError(f"{prefix} text must be nonblank and at most 2,000 UTF-8 bytes.")
        active = item.get("active", True)
        source = item.get("source")
        if type(active) is not bool:
            raise InputError(f"{prefix} active must be a boolean.")
        if source is not None and (not isinstance(source, str) or not source.strip()):
            raise InputError(f"{prefix} source must be a nonblank string when supplied.")
        result.append(Constraint(name, text.strip(), active, source))
        ids.add(name)
    if not any(c.active for c in result):
        raise InputError("At least one constraint must be active; an empty check cannot pass CI.")
    return result


def read_text(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise InputError(
                f"Input exceeds the {limit:,}-byte application limit; it was not truncated."
            )
        return content.decode("utf-8")
    except (OSError, UnicodeError):
        raise InputError("Could not read input as a UTF-8 file.") from None


def load_constraints(path: str | Path) -> list[Constraint]:
    return parse_constraints(strict_json(read_text(Path(path), 300_000)))


def passages_for(context: str) -> list[Passage]:
    if not isinstance(context, str):
        raise InputError("Context must be a string.")
    if len(context.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise InputError(
            f"Context exceeds {MAX_CONTEXT_BYTES:,} UTF-8 bytes; it was not truncated."
        )
    # Group consecutive lines when necessary, preserving ALL supplied text.
    lines = context.splitlines(keepends=True)
    if not context.strip():
        return []
    group_size = max(1, (len(lines) + MAX_PASSAGES - 1) // MAX_PASSAGES)
    return [
        Passage(
            f"p{offset // group_size + 1}",
            offset + 1,
            min(offset + group_size, len(lines)),
            "".join(lines[offset : offset + group_size]),
        )
        for offset in range(0, len(lines), group_size)
    ]
