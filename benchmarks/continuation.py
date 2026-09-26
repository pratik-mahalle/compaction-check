"""Run from the repository root after installing compaction-check."""

import argparse
import sys
from pathlib import Path

from compaction_check.inputs import InputError
from compaction_check.jev import DEFAULT_MODEL, JevClient, ProviderError
from compaction_check.sample_agent import comparison_markdown, run_experiment


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Live Jev agent with disposable local CSV tools. Uses API credits."
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="New or empty artifact directory"
    )
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--cycles", type=int, nargs="+", default=[2])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)
    try:
        # No retries: one logical evaluation corresponds to one billed API request.
        client = JevClient(model=args.model, retries=0)
        report = run_experiment(
            args.output,
            client,
            client,
            repetitions=args.repetitions,
            cycles=args.cycles,
            on_progress=lambda line: print(line, flush=True),
        )
    except (InputError, ProviderError, OSError) as exc:
        print(f"Experiment failed: {exc}", file=sys.stderr)
        return 2
    print(comparison_markdown(report))
    # Unprotected violations are expected measurements, not runner failures.
    return 2 if any(row["operational_errors"] for row in report["summary"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
