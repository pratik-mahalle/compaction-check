"""Regenerate self-contained JSON Schemas; no runtime dependency is required."""

import json
from pathlib import Path

IDENTIFIER = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"}
CONSTRAINT_ID = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"}
OPTIONAL_ID = {"anyOf": [IDENTIFIER, {"type": "null"}]}
NONNEGATIVE = {"type": "integer", "minimum": 0}


def object_schema(properties, required=None):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties) if required is None else required,
    }


message = object_schema(
    {
        "id": IDENTIFIER,
        "role": {"enum": ["system", "developer", "user", "assistant", "tool"]},
        "content": {"type": "string", "maxLength": 1_000_000},
        "sequence": NONNEGATIVE,
        "source_type": {
            "enum": [
                "instruction",
                "conversation",
                "summary",
                "tool_result",
                "reference",
                "restored_constraint",
            ]
        },
        "registry_id": OPTIONAL_ID,
        "constraint_id": OPTIONAL_ID,
        "constraint_version": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]},
    },
    ["id", "role", "content", "sequence"],
)
message["allOf"] = [
    {
        "if": {
            "properties": {"source_type": {"const": "restored_constraint"}},
            "required": ["source_type"],
        },
        "then": {
            "properties": {
                "registry_id": IDENTIFIER,
                "constraint_id": IDENTIFIER,
                "constraint_version": {"type": "integer", "minimum": 1},
            },
            "required": ["registry_id", "constraint_id", "constraint_version"],
        },
        "else": {
            "properties": {
                "registry_id": {"type": "null"},
                "constraint_id": {"type": "null"},
                "constraint_version": {"type": "null"},
            }
        },
    },
    {
        "if": {
            "properties": {"source_type": {"const": "instruction"}},
            "required": ["source_type"],
        },
        "then": {"properties": {"role": {"enum": ["system", "developer", "user"]}}},
    },
]
messages = {"type": "array", "maxItems": 10_000, "items": {"$ref": "#/$defs/message"}}
record = object_schema(
    {
        "id": CONSTRAINT_ID,
        "text": {"type": "string", "minLength": 1, "maxLength": 2_000},
        "version": {"type": "integer", "minimum": 1},
        "source": {"$ref": "#/$defs/message"},
        "span_start": NONNEGATIVE,
        "span_end": {"type": "integer", "minimum": 1},
        "task_id": OPTIONAL_ID,
        "created_revision": {"type": "integer", "minimum": 1},
        "status": {"enum": ["active", "superseded", "revoked", "ended"]},
        "ended_revision": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]},
    }
)
registry = object_schema(
    {
        "version": {"const": 2},
        "registry_id": IDENTIFIER,
        "session_id": IDENTIFIER,
        "revision": NONNEGATIVE,
        "task_id": OPTIONAL_ID,
        "closed_tasks": {"type": "array", "items": IDENTIFIER, "uniqueItems": True},
        "records": {"type": "array", "items": {"$ref": "#/$defs/record"}},
    }
)
trace = object_schema(
    {
        "version": {"const": 1},
        "kind": {"const": "compaction_trace"},
        "boundary_id": IDENTIFIER,
        "origin": {"enum": ["synthetic", "observed"]},
        "before": messages,
        "after": messages,
        "registry": {"$ref": "#/$defs/registry"},
        "expected": {
            "type": "object",
            "propertyNames": CONSTRAINT_ID,
            "additionalProperties": {
                "enum": ["preserved", "missing", "weakened", "contradicted", "uncertain"]
            },
        },
        "before_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "after_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
    }
)


def main():
    for name, title, schema, definitions in (
        ("messages-v1", "Complete structured message context", messages, {"message": message}),
        (
            "registry-v2",
            "Version 2 registry snapshot",
            registry,
            {"message": message, "record": record},
        ),
        (
            "trace-v1",
            "Version 1 compaction trace",
            trace,
            {"message": message, "record": record, "registry": registry},
        ),
    ):
        document = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": title,
            **schema,
            "$defs": definitions,
        }
        (Path(__file__).parent / f"{name}.schema.json").write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
