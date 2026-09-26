import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_registry_boundary import LiteralTestJudge, RegistryFixture

from compaction_check.cli import main
from compaction_check.inputs import InputError
from compaction_check.jev import Choice, Decision
from compaction_check.sample_agent import (
    DATASETS,
    behavioral_oracle,
    choose_action,
    execute_action,
    fixture_files,
    run_experiment,
    source_hashes,
)
from compaction_check.traces import load_trace


class FixtureActor:
    """Deterministic test double for integration mechanics, never reported as live data."""

    model = "unit-test-only"

    def __init__(self, stop=False):
        self.states = []
        self.stop = stop

    def evaluate(self, state, questions):
        self.states.append(state)
        selected = "baseline.csv"
        for message in state["messages"]:
            if message["role"] == "user":
                for name in DATASETS:
                    if f"Use only {name}" in message["content"]:
                        selected = name
        if self.stop:
            selected = "stop"
        return Decision(
            {
                "action": Choice(
                    selected,
                    1.0,
                    {k: float(k == selected) for k in questions["action"]["criteria"]},
                )
            },
            self.model,
            {"input_tokens": 20},
        )


class SampleTests(unittest.TestCase):
    def test_all_conditions_execute_tools_and_rule_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "experiment"
            actor = FixtureActor()
            report = run_experiment(output, actor, LiteralTestJudge(), repetitions=1, cycles=(2,))
            self.assertEqual(len(report["steps"]), 8)
            self.assertEqual(report["summary"]["unprotected"]["violations_all_steps"], 2)
            for mode in ("full_context", "pin", "repair"):
                self.assertEqual(report["summary"][mode]["compliant_completions"], 2)
                cycle2 = next(r for r in report["steps"] if r["mode"] == mode and r["cycle"] == 2)
                self.assertEqual(cycle2["oracle"]["actual_reads"], ["approved_b.csv"])
                self.assertEqual(cycle2["artifact"]["total_revenue"], 100)
            self.assertEqual(report["summary"]["pin"]["boundary_requests"], 2)
            self.assertEqual(report["summary"]["repair"]["boundary_requests"], 4)
            for row in report["steps"]:
                trace = load_trace(output / row["trace"])
                self.assertEqual(trace.origin, "synthetic")
            manifest = json.loads((output / "corpus.json").read_text())
            self.assertEqual(manifest["observed_boundaries"], 0)
            with self.assertRaises(InputError):
                run_experiment(output, actor, LiteralTestJudge(), repetitions=1)

    def test_stops_never_count_as_compliant_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            report = run_experiment(
                Path(temp) / "stopped",
                FixtureActor(stop=True),
                LiteralTestJudge(),
                repetitions=1,
                cycles=(2,),
            )
            for mode in report["summary"].values():
                self.assertEqual(mode["scheduled_steps"], 2)
                self.assertEqual(mode["attempted_steps"], 1)
                self.assertEqual(mode["completed_steps"], 0)
                self.assertEqual(mode["compliant_completions"], 0)
                self.assertEqual(mode["stops"], 2)
                self.assertEqual(mode["actor_requests"], 1)

    def test_actor_does_not_receive_oracle_or_registry(self):
        actor = FixtureActor()
        choose_action((), actor)
        self.assertEqual(actor.states, [{"messages": []}])

    def test_oracle_checks_actual_reads_artifacts_and_source_integrity(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            fixture_files(directory)
            before = source_hashes(directory)
            events, artifact = execute_action(directory, "baseline.csv")
            result = behavioral_oracle(
                "approved_a.csv", events, artifact, before, source_hashes(directory)
            )
            self.assertTrue(result["task_completed"])
            self.assertTrue(result["constraint_violated"])
            self.assertFalse(result["compliant_completion"])
            artifact["total_revenue"] = 40
            self.assertFalse(
                behavioral_oracle("approved_a.csv", events, artifact, before, before)[
                    "task_completed"
                ]
            )
            events, artifact = execute_action(directory, "approved_a.csv")
            (directory / "approved_a.csv").write_text("modified")
            self.assertFalse(
                behavioral_oracle(
                    "approved_a.csv", events, artifact, before, source_hashes(directory)
                )["compliant_completion"]
            )


class PrepareCliTests(RegistryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.add()
        self.messages = Path(self.temp.name) / "messages.json"
        self.messages.write_text("[]")
        self.output = Path(self.temp.name) / "result.json"

    def invoke(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()):
            return main(
                [
                    "prepare",
                    "--registry",
                    str(self.path),
                    "--messages",
                    str(self.messages),
                    "--format",
                    "json",
                    "--output",
                    str(self.output),
                    *extra,
                ]
            )

    def test_stale_revision_does_not_call_model(self):
        with patch("compaction_check.cli.JevClient", side_effect=AssertionError("No model call")):
            self.assertEqual(self.invoke("--expected-revision", "0"), 2)
        self.assertEqual(json.loads(self.output.read_text())["status"], "stale")

    def test_prepare_exit_codes_and_atomic_report(self):
        with patch("compaction_check.cli.JevClient", return_value=LiteralTestJudge()):
            self.assertEqual(self.invoke("--expected-revision", "1", "--mode", "pin"), 0)
            self.assertEqual(json.loads(self.output.read_text())["status"], "ready")
            self.assertEqual(self.invoke("--expected-revision", "1", "--mode", "audit"), 1)
            self.assertEqual(json.loads(self.output.read_text())["status"], "needs_review")

    def test_output_cannot_replace_registry(self):
        self.output = self.path
        self.assertEqual(self.invoke("--expected-revision", "1"), 2)
        self.assertEqual(self.registry.revision, 1)


if __name__ == "__main__":
    unittest.main()
