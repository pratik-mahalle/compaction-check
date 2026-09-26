"""Command-line interface with explicit CI exit codes and atomic JSON artifacts."""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from . import __version__
from .boundary import Budget, prepare_resume
from .checker import check
from .inputs import MAX_CONTEXT_BYTES, InputError, load_constraints, read_text, strict_json
from .jev import DEFAULT_MODEL, JevClient, ProviderError
from .messages import Message
from .registry import Registry
from .traces import load_trace


def _terminal(text):
    # Snapshot content is untrusted and may contain terminal control characters.
    return "".join(c if c.isprintable() else " " for c in str(text))


def format_text(report):
    if not report["complete"]:
        return "ERROR · check incomplete\n" + report["error"] + "\n"
    counts = report["summary"]
    lines = [
        f"{'PASS' if report['passed'] else 'FAIL'} · {counts['preserved']}/{counts['active']} active constraints preserved",
        f"Judge: {', '.join(report['resolved_models']) or report['judge']} | confidence threshold: {report['min_confidence']:.2f}",
        "",
    ]
    for result in report["results"]:
        confidence = (
            f" (confidence {result['confidence']:.2f})" if result["confidence"] is not None else ""
        )
        lines.append(f"{result['status'].upper():12} {result['id']}{confidence}")
        lines.append("  Expected: " + _terminal(result["constraint"]))
        lines.append("  " + result["reason"])
        if result["evidence"]:
            evidence = result["evidence"]
            lines.append(
                f"  Context lines {evidence['start_line']}–{evidence['end_line']}: "
                + _terminal(evidence["text"])
            )
        if result["raw_status"] and result["raw_status"] != result["status"]:
            lines.append("  Raw Jev judgment: " + result["raw_status"])
        lines.append("")
    lines.append("Checks retention in supplied context; does not prove agent compliance.")
    return "\n".join(lines) + "\n"


def write_report(path: Path, report):
    """Write atomically, with restrictive permissions because evidence may be private."""
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def parser():
    root = argparse.ArgumentParser(
        prog="compaction-check", description="Check constraint retention after compaction with Jev."
    )
    root.add_argument("--version", action="version", version=f"compaction-check {__version__}")
    sub = root.add_subparsers(dest="command", required=True)
    run = sub.add_parser(
        "check", help="Check an explicit constraint file against a complete context snapshot"
    )
    run.add_argument(
        "--constraints",
        required=True,
        type=Path,
        help="Version 1 constraints or version 2 registry snapshot JSON",
    )
    run.add_argument(
        "--context", required=True, type=Path, help="Full post-compaction context as UTF-8 text"
    )
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument(
        "--min-confidence",
        type=float,
        default=0.6,
        help="Status and supporting-passage confidence threshold (default: 0.6)",
    )
    run.add_argument("--timeout", type=float, default=30, help="Seconds per request (max: 60)")
    run.add_argument("--retries", type=int, default=2, help="Retries for HTTP 429/529 only (0–3)")
    run.add_argument("--format", choices=("text", "json"), default="text")
    run.add_argument("--output", type=Path, help="Also write a JSON report to this path")
    prepare = sub.add_parser(
        "prepare", help="Assess or restore structured messages using a persistent registry"
    )
    prepare.add_argument("--registry", type=Path, required=True)
    prepare.add_argument(
        "--messages",
        type=Path,
        required=True,
        help="JSON array containing the complete reconstructed messages",
    )
    prepare.add_argument("--mode", choices=("audit", "pin", "repair"), default="audit")
    prepare.add_argument("--task-id")
    prepare.add_argument(
        "--expected-revision", type=int, required=True, help="Revision captured before compaction"
    )
    prepare.add_argument("--model", default=DEFAULT_MODEL)
    prepare.add_argument("--min-confidence", type=float, default=0.6)
    prepare.add_argument("--max-requests", type=int, default=20)
    prepare.add_argument("--max-seconds", type=float, default=30)
    prepare.add_argument("--max-input-tokens", type=int)
    prepare.add_argument("--format", choices=("text", "json"), default="text")
    prepare.add_argument("--output", type=Path)
    trace = sub.add_parser(
        "trace", help="Validate a complete trace and its hashes without an API call"
    )
    trace.add_argument("path", type=Path)
    trace.add_argument("--format", choices=("text", "json"), default="text")
    trace.add_argument("--output", type=Path, help="Write the validated canonical trace")
    return root


def _prepare(args):
    registry = Registry(args.registry)
    data = strict_json(read_text(args.messages, 10_000_000))
    if not isinstance(data, list):
        raise InputError("Messages input must be a JSON array.")
    messages = tuple(Message.from_dict(item) for item in data)
    budget = Budget(args.max_requests, args.max_seconds, args.max_input_tokens)
    if args.expected_revision < 0:
        raise InputError("Expected revision must be nonnegative.")
    # Stale inputs and empty registries do not need credentials or a model call.
    judge = (
        JevClient(model=args.model, retries=0)
        if registry.revision == args.expected_revision and registry.snapshot(args.task_id).active
        else None
    )
    prepared = prepare_resume(
        messages,
        registry,
        judge,
        mode=args.mode,
        task_id=args.task_id,
        expected_revision=args.expected_revision,
        budget=budget,
        min_confidence=args.min_confidence,
    )
    report = prepared.to_dict()
    report["exit_code"] = 0 if prepared.ready else 1 if prepared.status == "needs_review" else 2
    return report


def main(argv=None):
    args = parser().parse_args(argv)
    protected = (
        {args.constraints.resolve(), args.context.resolve()}
        if args.command == "check"
        else {
            args.registry.resolve(),
            args.messages.resolve(),
            Path(str(args.registry) + "-wal").resolve(),
            Path(str(args.registry) + "-shm").resolve(),
            Path(str(args.registry) + "-journal").resolve(),
        }
        if args.command == "prepare"
        else {args.path.resolve()}
    )
    output_conflict = args.output and args.output.resolve() in protected
    try:
        if output_conflict:
            raise InputError("Report output must not overwrite an input file or registry sidecar.")
        if args.command == "prepare":
            report = _prepare(args)
        elif args.command == "trace":
            report = load_trace(args.path).to_dict()
        else:
            constraints = load_constraints(args.constraints)
            context = read_text(args.context, MAX_CONTEXT_BYTES)
            judge = (
                JevClient(model=args.model, timeout=args.timeout, retries=args.retries)
                if context.strip()
                else None
            )
            report = check(constraints, context, judge, min_confidence=args.min_confidence)
    except (InputError, ProviderError, OSError, sqlite3.Error) as exc:
        report = {
            "schema_version": 1,
            "complete": False,
            "passed": False,
            "exit_code": 2,
            "error": str(exc)
            if isinstance(exc, (InputError, ProviderError))
            else "Could not access a file or registry.",
        }
    if args.output:
        # Do not write an error artifact on top of a protected input either.
        if output_conflict:
            report = {
                "schema_version": 1,
                "complete": False,
                "passed": False,
                "exit_code": 2,
                "error": "Report output must not overwrite an input file or registry sidecar.",
            }
        else:
            try:
                write_report(args.output, report)
            except OSError:
                report = {
                    "schema_version": 1,
                    "complete": False,
                    "passed": False,
                    "exit_code": 2,
                    "error": "Could not write the JSON report.",
                }
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    elif report.get("kind") == "prepared_resume":
        print(
            f"{report['status'].upper()} · {report['reason']}\nRegistry revision: {report['registry_revision']} | requests: {report['metrics']['requests']}"
        )
    elif report.get("kind") == "compaction_trace":
        print(
            f"VALID · {report['origin']} trace {report['boundary_id']} | session: {report['registry']['session_id']}"
        )
    else:
        print(
            format_text(report), end="", file=sys.stderr if report["exit_code"] == 2 else sys.stdout
        )
    return report.get("exit_code", 0)
