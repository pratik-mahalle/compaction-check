"""Recompute comparison metrics from saved tool events; makes no API calls."""

import argparse
import hashlib
import json
from pathlib import Path

from compaction_check.cli import write_report
from compaction_check.inputs import InputError, read_text, strict_json
from compaction_check.sample_agent import behavioral_oracle, comparison_markdown, summarize


def combine(paths):
    reports, sources, rows, seen = [], [], [], set()
    for path in paths:
        raw = read_text(path, 50_000_000)
        report = strict_json(raw)
        if report.get("kind") != "local_continuation" or report.get("origin") != "synthetic":
            raise InputError("Expected a local synthetic continuation report.")
        reports.append(report)
        sources.append({"path": str(path), "sha256": hashlib.sha256(raw.encode()).hexdigest()})
        for row in report["steps"]:
            identity = (row["session_id"], row["cycle"])
            if identity in seen:
                raise InputError("Reports contain duplicate steps; do not count a run twice.")
            seen.add(identity)
            checked = behavioral_oracle(
                row["oracle"]["expected_dataset"],
                row["tool_events"],
                row["artifact"],
                row["source_hashes_before"],
                row["source_hashes_after"],
            )
            if checked != row["oracle"]:
                raise InputError(
                    "Saved behavioral judgment disagrees with its tool events or artifact."
                )
            rows.append(row)
    first = reports[0]
    for report in reports:
        for key in ("model", "judge_model", "repetitions", "fixture_version"):
            if report[key] != first[key]:
                raise InputError("Combine only runs with matching model and fixture settings.")
    return {
        "schema_version": 1,
        "kind": "continuation_comparison",
        "origin": "synthetic",
        "model": first["model"],
        "judge_model": first["judge_model"],
        "fixture_version": first["fixture_version"],
        "repetitions": first["repetitions"],
        "cycles": sorted({c for report in reports for c in report["cycles"]}),
        "sources": sources,
        "summary": summarize(rows),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New summary JSON path; also writes a sibling Markdown file",
    )
    args = parser.parse_args()
    if args.output.exists() or args.output.with_suffix(".md").exists():
        parser.error("Choose new output paths.")
    try:
        combined = combine(args.reports)
    except (InputError, OSError, KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    write_report(args.output, combined)
    markdown = comparison_markdown(combined)
    args.output.with_suffix(".md").write_text(markdown, encoding="utf-8")
    print(json.dumps(combined["summary"], indent=2))


if __name__ == "__main__":
    main()
