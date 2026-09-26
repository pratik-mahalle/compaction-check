"""SQLite event log for explicit, reviewed constraint lifecycle updates."""

import json
import os
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .inputs import Constraint, InputError
from .messages import AUTHORITIES, Message, digest, identifier


class RegistryConflict(InputError):
    pass


@dataclass(frozen=True)
class Record:
    id: str
    text: str
    version: int
    source: Message
    span_start: int
    span_end: int
    task_id: str | None
    created_revision: int
    status: str = "active"
    ended_revision: int | None = None

    @property
    def role(self):
        return self.source.role

    def applies(self, task_id):
        return self.status == "active" and (self.task_id is None or self.task_id == task_id)

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Snapshot:
    registry_id: str
    session_id: str
    revision: int
    records: tuple[Record, ...] = ()
    closed_tasks: tuple[str, ...] = ()
    task_id: str | None = None

    @property
    def active(self):
        return tuple(r for r in self.records if r.applies(self.task_id))

    def constraints(self):
        return [
            Constraint(r.id, r.text, source=f"{r.source.id}:{r.span_start}-{r.span_end}")
            for r in self.active
        ]

    def to_dict(self):
        return {
            "version": 2,
            "registry_id": self.registry_id,
            "session_id": self.session_id,
            "revision": self.revision,
            "task_id": self.task_id,
            "closed_tasks": list(self.closed_tasks),
            "records": [r.to_dict() for r in self.records],
        }

    @property
    def sha256(self):
        return digest(self.to_dict())


def _source_spec(id, source, span, task_id):
    identifier(id, "Constraint id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", id):
        raise InputError(
            "Constraint ids must be 1–64 letters, digits, dots, dashes or underscores."
        )
    if task_id is not None:
        identifier(task_id, "Task id")
    if (
        not isinstance(source, Message)
        or source.role not in AUTHORITIES
        or source.source_type != "instruction"
    ):
        raise InputError(
            "Register a reviewed direct instruction from a system, developer or user message."
        )
    if span is not None and (not isinstance(span, (list, tuple)) or len(span) != 2):
        raise InputError("Source span must contain start and end offsets.")
    start, end = (0, len(source.content)) if span is None else span
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(source.content)
    ):
        raise InputError("Source span must be a nonempty character range in the source message.")
    text = source.content[start:end]
    if not text.strip() or text != text.strip() or len(text.encode()) > 2_000:
        raise InputError(
            "Registered wording must be trimmed, nonempty and at most 2,000 UTF-8 bytes."
        )
    return {
        "id": id,
        "text": text,
        "source": source.to_dict(),
        "span_start": start,
        "span_end": end,
        "task_id": task_id,
    }


def _apply(snapshot, operation, payload, revision):
    records = list(snapshot.records)
    closed = list(snapshot.closed_tasks)
    if operation in {"register", "supersede"}:
        source = Message.from_dict(payload["source"])
        spec = _source_spec(
            payload["id"], source, (payload["span_start"], payload["span_end"]), payload["task_id"]
        )
        if spec != payload:
            raise InputError("Registry source evidence does not match the registered text.")
        if any(r.source.id == source.id and r.source != source for r in records):
            raise RegistryConflict(
                "A source message id cannot refer to different original messages."
            )
        if payload["task_id"] in closed:
            raise RegistryConflict("Cannot register a rule in a completed task scope.")
        previous = [r for r in records if r.id == payload["id"]]
        version = 1
        if operation == "register" and previous:
            raise RegistryConflict(
                "Constraint id already exists; use supersede for an active rule."
            )
        if operation == "supersede":
            if not previous or previous[-1].status != "active":
                raise RegistryConflict("Only an active constraint can be superseded.")
            old = previous[-1]
            if old.role != source.role or old.task_id != payload["task_id"]:
                raise RegistryConflict("Superseding a rule cannot change its authority or scope.")
            if source.id == old.source.id or source.sequence <= old.source.sequence:
                raise RegistryConflict(
                    "A superseding instruction must come from a later source message."
                )
            records[records.index(old)] = replace(old, status="superseded", ended_revision=revision)
            version = old.version + 1
        records.append(
            Record(
                payload["id"],
                payload["text"],
                version,
                source,
                payload["span_start"],
                payload["span_end"],
                payload["task_id"],
                revision,
            )
        )
        if sum(r.status == "active" for r in records) > 100:
            raise InputError("A registry supports at most 100 active constraints.")
    elif operation == "revoke":
        active = [r for r in records if r.id == payload["id"] and r.status == "active"]
        if len(active) != 1:
            raise RegistryConflict("Only an active constraint can be revoked.")
        record = active[0]
        records[records.index(record)] = replace(record, status="revoked", ended_revision=revision)
    elif operation == "end_scope":
        task_id = payload["task_id"]
        if task_id in closed:
            raise RegistryConflict("Task scope is already closed.")
        closed.append(task_id)
        records = [
            replace(r, status="ended", ended_revision=revision)
            if r.status == "active" and r.task_id == task_id
            else r
            for r in records
        ]
    else:
        raise InputError("Unknown registry operation.")
    return replace(snapshot, revision=revision, records=tuple(records), closed_tasks=tuple(closed))


class Registry:
    """One session per SQLite file. Each mutation is atomic and supports event-id deduplication."""

    def __init__(self, path: str | Path, session_id: str | None = None):
        self.path = Path(path)
        if str(path) == ":memory:":
            raise InputError("Use a file-backed registry so revisions survive restarts.")
        if session_id is not None:
            identifier(session_id, "Session id")
        if not self.path.exists() and session_id is None:
            raise InputError("A session id is required to create a registry.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with self._connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS events (revision INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, operation TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            connection.execute("BEGIN IMMEDIATE")
            saved = dict(connection.execute("SELECT key, value FROM metadata"))
            if not saved:
                if session_id is None:
                    raise InputError("Registry has no session metadata.")
                saved = {"session_id": session_id, "registry_id": uuid.uuid4().hex}
                connection.executemany("INSERT INTO metadata VALUES (?, ?)", saved.items())
            if set(saved) != {"session_id", "registry_id"}:
                raise InputError("Invalid registry metadata.")
            if session_id is not None and session_id != saved["session_id"]:
                raise RegistryConflict("This registry belongs to a different session.")
            self.session_id, self.registry_id = saved["session_id"], saved["registry_id"]

    def _connection(self):
        # Connection context commits/rolls back; closing is handled explicitly by the wrapper.
        from contextlib import contextmanager

        @contextmanager
        def connect():
            connection = sqlite3.connect(self.path, timeout=5)
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

        return connect()

    def _replay(self, connection, revision=None, task_id=None):
        snapshot = Snapshot(self.registry_id, self.session_id, 0, task_id=task_id)
        query = "SELECT revision, operation, payload FROM events"
        args = ()
        if revision is not None:
            query += " WHERE revision <= ?"
            args = (revision,)
        for number, operation, payload in connection.execute(query + " ORDER BY revision", args):
            if number != snapshot.revision + 1:
                raise InputError("Registry event history has a revision gap.")
            snapshot = _apply(snapshot, operation, json.loads(payload), number)
        if revision is not None and revision != snapshot.revision:
            raise InputError("Requested registry revision does not exist.")
        return snapshot

    def snapshot(self, task_id=None, *, revision=None):
        if task_id is not None:
            identifier(task_id, "Task id")
        if revision is not None and (type(revision) is not int or revision < 0):
            raise InputError("Registry revision must be a nonnegative integer.")
        with self._connection() as connection:
            return self._replay(connection, revision, task_id)

    @property
    def revision(self):
        with self._connection() as connection:
            return connection.execute("SELECT COALESCE(MAX(revision), 0) FROM events").fetchone()[0]

    def assert_revision(self, expected):
        if self.revision != expected:
            raise RegistryConflict("Registry changed; rebuild and recheck the resume context.")

    def _commit(self, operation, payload, event_id, expected_revision):
        identifier(event_id, "Event id")
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise InputError("Expected revision must be a nonnegative integer.")
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT operation, payload FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            snapshot = self._replay(connection)
            if duplicate:
                if duplicate != (operation, encoded):
                    raise RegistryConflict("An event id was reused with different content.")
                return snapshot
            if expected_revision is not None and snapshot.revision != expected_revision:
                raise RegistryConflict("Registry revision changed before this update.")
            updated = _apply(snapshot, operation, payload, snapshot.revision + 1)
            connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?)",
                (updated.revision, event_id, operation, encoded),
            )
            return updated

    def register(self, id, source, *, event_id, span=None, task_id=None, expected_revision=None):
        return self._commit(
            "register", _source_spec(id, source, span, task_id), event_id, expected_revision
        )

    def supersede(self, id, source, *, event_id, span=None, task_id=None, expected_revision=None):
        return self._commit(
            "supersede", _source_spec(id, source, span, task_id), event_id, expected_revision
        )

    def revoke(self, id, *, event_id, reason, expected_revision=None):
        identifier(id, "Constraint id")
        if not isinstance(reason, str) or not reason.strip():
            raise InputError("Provide a reason for revoking a constraint.")
        return self._commit("revoke", {"id": id, "reason": reason}, event_id, expected_revision)

    def end_scope(self, task_id, *, event_id, expected_revision=None):
        identifier(task_id, "Task id")
        return self._commit("end_scope", {"task_id": task_id}, event_id, expected_revision)


def snapshot_from_dict(data):
    fields = {
        "version",
        "registry_id",
        "session_id",
        "revision",
        "task_id",
        "closed_tasks",
        "records",
    }
    if (
        not isinstance(data, dict)
        or set(data) != fields
        or type(data["version"]) is not int
        or data["version"] != 2
    ):
        raise InputError("Expected a version 2 registry snapshot.")
    identifier(data["registry_id"], "Registry id")
    identifier(data["session_id"], "Session id")
    if data["task_id"] is not None:
        identifier(data["task_id"], "Task id")
    revision = data["revision"]
    if type(revision) is not int or revision < 0 or not isinstance(data["records"], list):
        raise InputError("Invalid snapshot revision or records.")
    if (
        not isinstance(data["closed_tasks"], list)
        or not all(isinstance(t, str) for t in data["closed_tasks"])
        or len(set(data["closed_tasks"])) != len(data["closed_tasks"])
    ):
        raise InputError("Closed task scopes must be a unique list.")
    for task in data["closed_tasks"]:
        identifier(task, "Task id")
    records, versions, active_ids = [], {}, set()
    for item in data["records"]:
        try:
            record = Record(**{**item, "source": Message.from_dict(item["source"])})
        except (KeyError, TypeError):
            raise InputError("Malformed registry record.") from None
        spec = _source_spec(
            record.id, record.source, (record.span_start, record.span_end), record.task_id
        )
        if (
            spec["text"] != record.text
            or type(record.version) is not int
            or record.version != versions.get(record.id, 0) + 1
        ):
            raise InputError("Record wording or version history is invalid.")
        previous = next((r for r in reversed(records) if r.id == record.id), None)
        if previous and (
            previous.status != "superseded"
            or previous.ended_revision != record.created_revision
            or previous.role != record.role
            or previous.task_id != record.task_id
            or previous.source.sequence >= record.source.sequence
        ):
            raise InputError("Snapshot contains an invalid supersession chain.")
        if any(r.source.id == record.source.id and r.source != record.source for r in records):
            raise InputError("Snapshot contains inconsistent source messages.")
        if type(record.created_revision) is not int or not 1 <= record.created_revision <= revision:
            raise InputError("Record creation revision is invalid.")
        if not isinstance(record.status, str) or record.status not in {
            "active",
            "superseded",
            "revoked",
            "ended",
        }:
            raise InputError("Unknown record status.")
        if record.status == "active":
            if (
                record.id in active_ids
                or record.ended_revision is not None
                or record.task_id in data["closed_tasks"]
            ):
                raise InputError("Active record conflicts with the snapshot lifecycle.")
            active_ids.add(record.id)
        elif (
            type(record.ended_revision) is not int
            or not record.created_revision < record.ended_revision <= revision
        ):
            raise InputError("Record end revision is invalid.")
        versions[record.id] = record.version
        records.append(record)
    for record in records:
        if record.status == "superseded" and record.version == versions[record.id]:
            raise InputError("Superseded record has no replacement.")
        if record.status == "ended" and record.task_id not in data["closed_tasks"]:
            raise InputError("Ended record does not belong to a closed task scope.")
    return Snapshot(
        data["registry_id"],
        data["session_id"],
        revision,
        tuple(records),
        tuple(data["closed_tasks"]),
        data["task_id"],
    )
