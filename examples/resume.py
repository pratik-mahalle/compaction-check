"""Two compactions and a changed user requirement, using a local registry and Jev."""

import tempfile
from pathlib import Path

from compaction_check import JevClient, Message, Registry, compact_and_prepare
from compaction_check.messages import resequence


def compact(messages):
    # Demonstration fault injection: the real host supplies its own summarizer.
    kept = [m for m in messages if m.source_type == "restored_constraint"]
    return resequence(
        [*kept, Message("summary", "assistant", "Continue analyzing sales.", 0, "summary")]
    )


def main():
    judge = JevClient(retries=0)
    with tempfile.TemporaryDirectory() as directory:
        registry = Registry(Path(directory) / "registry.sqlite", "sample-session")
        current = ()
        for cycle, dataset in enumerate(("approved_a.csv", "approved_b.csv"), 1):
            source = Message(
                f"user-{cycle}", "user", f"Use only {dataset}.", cycle * 100, "instruction"
            )
            update = registry.register if cycle == 1 else registry.supersede
            update("dataset", source, event_id=f"update-{cycle}")
            prepared = compact_and_prepare(
                resequence([*current, source]), compact, registry, judge, mode="pin"
            )
            print(f"Cycle {cycle}, registry revision {registry.revision}: {prepared.status}")
            if not prepared.ready:
                print(prepared.reason)
                return 1
            current = prepared.messages_for(registry)
            print(
                "Restored:", [m.content for m in current if m.source_type == "restored_constraint"]
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
