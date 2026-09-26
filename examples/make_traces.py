"""Convert the original labelled examples into complete structured trace fixtures."""

import tempfile
from pathlib import Path

from compaction_check import Message, Registry, Trace
from compaction_check.cli import write_report
from compaction_check.traces import corpus_manifest


def main():
    examples = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as directory:
        registry = Registry(Path(directory) / "registry.sqlite", "synthetic-sales-example")
        messages = (
            Message(
                "user-1", "user", "Use only sales_2025.csv as the data source.", 1, "instruction"
            ),
            Message(
                "user-2",
                "user",
                "Ask the user for approval before deleting any file.",
                2,
                "instruction",
            ),
            Message("user-3", "user", "Write the final report in English.", 3, "instruction"),
            Message("user-5", "user", "Write the final report in Spanish.", 5, "instruction"),
        )
        for id, source in zip(("dataset", "approval", "language"), messages[:3]):
            registry.register(id, source, event_id=f"register-{id}")
        registry.supersede("language", messages[-1], event_id="switch-language")
        traces = []
        for name, labels in (
            ("good", ("preserved",) * 3),
            ("bad", ("missing", "weakened", "contradicted")),
        ):
            after = (
                Message(
                    f"summary-{name}",
                    "assistant",
                    (examples / f"compacted-{name}.md").read_text(),
                    0,
                    "summary",
                ),
            )
            trace = Trace(
                name,
                "synthetic",
                messages,
                after,
                registry.snapshot(),
                dict(zip(("dataset", "approval", "language"), labels)),
            )
            write_report(examples / "traces" / f"{name}.json", trace.to_dict())
            traces.append(trace)
        write_report(examples / "traces" / "corpus.json", corpus_manifest(traces))
    print("Wrote two synthetic traces; both variants belong to one development session.")


if __name__ == "__main__":
    main()
