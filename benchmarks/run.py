"""Opt-in, live Jev smoke benchmark. Uses synthetic examples and real API credits."""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from compaction_check import Constraint, JevClient, check
from compaction_check.checker import POLICY_VERSION
from compaction_check.cli import write_report
from compaction_check.inputs import InputError
from compaction_check.jev import DEFAULT_MODEL, ProviderError

ROOT = Path(__file__).resolve().parents[1]


def run(client, output, min_confidence=0.6):
    cases_path = ROOT / "benchmarks/cases.json"
    cases = json.loads(cases_path.read_text())
    sources = [
        cases_path,
        Path(__file__),
        ROOT / "src/compaction_check/checker.py",
        ROOT / "src/compaction_check/jev.py",
        ROOT / "src/compaction_check/inputs.py",
    ]
    report = {
        "kind": "live_jev_synthetic_retention_smoke",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "requested_model": client.model,
        "policy_version": POLICY_VERSION,
        "min_confidence": min_confidence,
        "complete": False,
        "source_sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources
        },
        "records": [],
    }
    for case in cases:
        try:
            result = check(
                [Constraint(case["id"], case["constraint"])],
                case["context"],
                client,
                min_confidence=min_confidence,
            )
        except (InputError, ProviderError) as exc:
            report["error"] = str(exc)
            write_report(output, report)
            print("Benchmark incomplete: " + str(exc), file=sys.stderr)
            return 2
        verdict = result["results"][0]
        report["records"].append(
            {
                "case": case["id"],
                "expected": case["expected"],
                "actual": verdict["status"],
                "check": result,
            }
        )
        write_report(output, report)
        print(f"{case['id']}: expected={case['expected']} actual={verdict['status']}", flush=True)
    records = report["records"]
    report["complete"] = True
    report["summary"] = {
        "cases": len(records),
        "exact_status_matches": sum(r["expected"] == r["actual"] for r in records),
        "false_passes": sum(
            r["expected"] != "preserved" and r["actual"] == "preserved" for r in records
        ),
        "false_alarms": sum(
            r["expected"] == "preserved" and r["actual"] != "preserved" for r in records
        ),
        "uncertain": sum(r["actual"] == "uncertain" for r in records),
        "api_calls": sum(len(r["check"]["calls"]) for r in records),
        "reported_input_tokens": sum(
            call["usage"].get("input_tokens", 0) for r in records for call in r["check"]["calls"]
        ),
    }
    write_report(output, report)
    print(json.dumps(report["summary"], indent=2))
    return 0 if report["summary"]["exact_status_matches"] == len(records) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/benchmark.json")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--min-confidence", type=float, default=0.6)
    args = parser.parse_args()
    try:
        client = JevClient(model=args.model)
        return run(client, args.output, args.min_confidence)
    except (InputError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
