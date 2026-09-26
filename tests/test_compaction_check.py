import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import Mock, patch

from compaction_check import Constraint, JevClient, check, parse_constraints
from compaction_check.checker import CRITERIA, build_request
from compaction_check.cli import format_text, main
from compaction_check.inputs import (
    MAX_CONTEXT_BYTES,
    InputError,
    load_constraints,
    passages_for,
    strict_json,
)
from compaction_check.jev import (
    ENDPOINT,
    Choice,
    Decision,
    ProviderError,
    _NoRedirect,
    _retry_delay,
    parse_decision,
)


def choice(label, options, confidence=0.9):
    return Choice(label, confidence, {o: float(o == label) for o in options})


class FakeJudge:
    """Transport double, not a semantic classifier. Live labels have a separate benchmark."""

    model = "fixture-jev"

    def __init__(self, status="preserved", evidence="p1", confidence=0.9, evidence_confidence=0.9):
        self.status, self.evidence = status, evidence
        self.confidence, self.evidence_confidence = confidence, evidence_confidence
        self.calls = []

    def evaluate(self, state, questions):
        self.calls.append((state, questions))
        return Decision(
            {
                "status": choice(self.status, questions["status"]["criteria"], self.confidence),
                "evidence": choice(
                    self.evidence, questions["evidence"]["criteria"], self.evidence_confidence
                ),
            },
            self.model,
            {"input_tokens": 100},
        )


class InputTests(unittest.TestCase):
    def document(self, **overrides):
        return {
            "version": 1,
            "constraints": [{"id": "data", "text": "Use only a.csv.", **overrides}],
        }

    def test_defaults_and_source(self):
        result = parse_constraints(self.document(source="turn 1"))
        self.assertEqual(result, [Constraint("data", "Use only a.csv.", source="turn 1")])

    def test_bad_documents(self):
        bad = [
            [],
            {},
            {"version": True, "constraints": []},
            self.document(active="false"),
            self.document(id="has spaces"),
            self.document(text=" "),
            self.document(typo=True),
            self.document(source=3),
            self.document(text="é" * 1001),
            self.document(active=False),
        ]
        for data in bad:
            with self.subTest(data=str(data)[:60]), self.assertRaises(InputError):
                parse_constraints(data)

    def test_duplicate_ids(self):
        data = self.document()
        data["constraints"] *= 2
        with self.assertRaises(InputError):
            parse_constraints(data)

    def test_strict_json_rejects_silent_overwrites_and_nonfinite_numbers(self):
        for text in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', "{oops}"):
            with self.subTest(text=text), self.assertRaises(InputError):
                strict_json(text)

    def test_evidence_is_verbatim_with_original_line_numbers(self):
        context = "heading\r\n\nA rule.\n"
        passages = passages_for(context)
        self.assertEqual("".join(p.text for p in passages), context)
        self.assertEqual(passages[2].start_line, 3)
        self.assertEqual(passages[2].text, "A rule.\n")

    def test_many_lines_are_grouped_without_losing_text(self):
        context = "first\n" + "filler\n" * 800 + "LAST RULE"
        passages = passages_for(context)
        self.assertLessEqual(len(passages), 128)
        self.assertEqual("".join(p.text for p in passages), context)
        self.assertEqual(passages[-1].end_line, 802)

    def test_oversized_unicode_context_is_rejected_without_truncation(self):
        with self.assertRaisesRegex(InputError, "not truncated"):
            passages_for("é" * MAX_CONTEXT_BYTES)

    def test_bad_utf8_is_a_clean_input_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_bytes(b"\xff")
            with self.assertRaises(InputError):
                load_constraints(path)


class CheckTests(unittest.TestCase):
    constraints = [Constraint("language", "Write the report in Spanish.")]

    def test_all_statuses_map_to_ci_policy(self):
        for status in CRITERIA:
            with self.subTest(status=status):
                report = check(self.constraints, "Use Spanish.", FakeJudge(status=status))
                self.assertEqual(report["passed"], status == "preserved")
                self.assertEqual(report["exit_code"], 0 if status == "preserved" else 1)
                self.assertEqual(report["results"][0]["status"], status)

    def test_low_confidence_does_not_pass(self):
        report = check(self.constraints, "Use Spanish.", FakeJudge(confidence=0.2))
        result = report["results"][0]
        self.assertFalse(report["passed"])
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["raw_status"], "preserved")

    def test_preserved_needs_confident_evidence(self):
        for judge in (FakeJudge(evidence="none"), FakeJudge(evidence_confidence=0.1)):
            with self.subTest(judge=judge):
                report = check(self.constraints, "Use Spanish.", judge)
                self.assertEqual(report["results"][0]["status"], "uncertain")

    def test_missing_does_not_require_an_evidence_passage(self):
        report = check(
            self.constraints, "Calculate totals.", FakeJudge(status="missing", evidence="none")
        )
        self.assertEqual(report["results"][0]["status"], "missing")
        self.assertIsNone(report["results"][0]["evidence"])

    def test_inactive_constraint_is_never_sent_to_model(self):
        constraints = self.constraints + [Constraint("old", "Write in English.", active=False)]
        judge = FakeJudge()
        report = check(constraints, "Use Spanish.", judge)
        self.assertEqual(len(judge.calls), 1)
        self.assertNotIn("Write in English", json.dumps(judge.calls))
        self.assertEqual(report["summary"]["skipped"], 1)
        self.assertTrue(report["passed"])

    def test_empty_context_is_missing_without_network(self):
        report = check(self.constraints, " \n")
        self.assertEqual(report["results"][0]["status"], "missing")
        self.assertFalse(report["passed"])
        self.assertEqual(report["calls"], [])

    def test_empty_or_all_inactive_cannot_pass(self):
        for constraints in ([], [replace(self.constraints[0], active=False)]):
            with self.assertRaises(InputError):
                check(constraints, "", FakeJudge())

    def test_invalid_thresholds(self):
        for threshold in (True, float("nan"), float("inf"), -0.1, 1.1):
            with self.subTest(threshold=threshold), self.assertRaises(InputError):
                check(self.constraints, "hello", FakeJudge(), min_confidence=threshold)

    def test_context_hash_detects_changes(self):
        a = check(self.constraints, "Use Spanish.", FakeJudge())
        b = check(self.constraints, "Use English.", FakeJudge())
        self.assertNotEqual(a["context_sha256"], b["context_sha256"])
        self.assertEqual(a["constraints_sha256"], b["constraints_sha256"])

    def test_reference_is_not_inserted_as_retained_context(self):
        state, questions = build_request(self.constraints[0], passages_for("Calculate totals."))
        self.assertNotIn("Spanish", json.dumps(state))
        self.assertIn("Spanish", questions["status"]["instructions"])
        self.assertIn("not evidence", questions["status"]["instructions"].lower())

    def test_terminal_output_strips_escapes(self):
        report = check(self.constraints, "\x1b[31mUse Spanish.\x1b[0m", FakeJudge())
        self.assertNotIn("\x1b", format_text(report))


class ProviderTests(unittest.TestCase):
    questions = {"status": {"type": "choice", "criteria": {"yes": "Yes", "no": "No"}}}

    def response(self):
        return {
            "model": "jev-1.13.0",
            "answers": {
                "status": {
                    "type": "choice",
                    "choice": "yes",
                    "confidence": 0.9,
                    "probabilities": {"yes": 0.95, "no": 0.05},
                }
            },
            "usage": {"input_tokens": 42},
        }

    def opener(self, body):
        response = Mock()
        response.read.return_value = body
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        return opener

    def test_native_request_uses_only_official_endpoint_and_auth_header(self):
        opener = self.opener(json.dumps(self.response()).encode())
        with patch("urllib.request.build_opener", return_value=opener):
            client = JevClient(api_key="test-secret")
            answer = client.evaluate({"text": "hello"}, self.questions)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertNotIn(b"test-secret", request.data)
        self.assertNotIn("test-secret", repr(client))
        self.assertEqual(json.loads(request.data)["model"], "jev-1.13.0")
        self.assertEqual(answer.answers["status"].choice, "yes")

    def test_schema_failures_are_not_success(self):
        mutations = [
            lambda d: d.pop("answers"),
            lambda d: d.update(model=None),
            lambda d: d["answers"]["status"].update(choice="invented"),
            lambda d: d["answers"]["status"].update(confidence=float("nan")),
            lambda d: d["answers"]["status"].update(confidence=True),
            lambda d: d["answers"]["status"].update(probabilities={"yes": 1}),
            lambda d: d["answers"]["status"].update(probabilities={"yes": 0.1, "no": 0.9}),
            lambda d: d["answers"]["status"].update(probabilities={"yes": 0.1, "no": 0.1}),
            lambda d: d.update(usage={"input_tokens": -1}),
        ]
        for mutate in mutations:
            data = self.response()
            mutate(data)
            with self.subTest(data=data), self.assertRaises(ProviderError):
                parse_decision(data, self.questions)

    def test_http_failure_is_sanitized(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            ENDPOINT, 401, "test-secret", {}, io.BytesIO(b"private response and test-secret")
        )
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ProviderError) as caught:
                JevClient(api_key="test-secret").evaluate({}, self.questions)
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_transient_retry_is_bounded_and_honors_retry_after(self):
        opener = self.opener(json.dumps(self.response()).encode())
        success = opener.open.return_value
        opener.open.side_effect = [
            urllib.error.HTTPError(ENDPOINT, 429, "slow down", {"Retry-After": "3"}, io.BytesIO()),
            success,
        ]
        with (
            patch("urllib.request.build_opener", return_value=opener),
            patch("time.sleep") as sleep,
        ):
            JevClient(api_key="test-secret").evaluate({}, self.questions)
        self.assertEqual(opener.open.call_count, 2)
        sleep.assert_called_once_with(3)

    def test_overload_exhausts_retry_budget(self):
        opener = Mock()
        opener.open.side_effect = [
            urllib.error.HTTPError(ENDPOINT, 529, "overloaded", {}, io.BytesIO()) for _ in range(3)
        ]
        with patch("urllib.request.build_opener", return_value=opener), patch("time.sleep"):
            with self.assertRaises(ProviderError):
                JevClient(api_key="test-secret").evaluate({}, self.questions)
        self.assertEqual(opener.open.call_count, 3)

    def test_network_timeouts_do_not_replay(self):
        opener = Mock()
        opener.open.side_effect = TimeoutError("test-secret")
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ProviderError) as caught:
                JevClient(api_key="test-secret").evaluate({}, self.questions)
        self.assertEqual(opener.open.call_count, 1)
        self.assertNotIn("test-secret", str(caught.exception))

    def test_invalid_json_is_a_clean_failure(self):
        with patch("urllib.request.build_opener", return_value=self.opener(b"not json")):
            with self.assertRaisesRegex(ProviderError, "invalid JSON"):
                JevClient(api_key="test-secret").evaluate({}, self.questions)

    def test_interrupted_http_body_is_a_clean_failure(self):
        opener = self.opener(b"")
        opener.open.return_value.read.side_effect = IncompleteRead(b"private response", 100)
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ProviderError) as caught:
                JevClient(api_key="test-secret").evaluate({}, self.questions)
        self.assertNotIn("private response", str(caught.exception))

    def test_broken_certificate_configuration_is_a_clean_failure(self):
        with patch("ssl.create_default_context", side_effect=OSError("private path")):
            with self.assertRaisesRegex(ProviderError, "certificate bundle"):
                JevClient(api_key="test-secret").evaluate({}, self.questions)

    def test_invalid_auth_header_characters_are_rejected(self):
        for key in ("secret\nheader", "secret\x00", "sëcret"):
            with self.subTest(key=repr(key)), self.assertRaises(InputError):
                JevClient(api_key=key)

    def test_auth_cannot_follow_redirect(self):
        self.assertIsNone(
            _NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test")
        )

    def test_excessive_retry_after_is_not_ignored(self):
        with self.assertRaises(ProviderError):
            _retry_delay("90", 0)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.constraints = self.root / "constraints.json"
        self.context = self.root / "context.txt"
        self.output = self.root / "report.json"
        self.constraints.write_text(
            json.dumps({"version": 1, "constraints": [{"id": "lang", "text": "Use Spanish."}]})
        )
        self.context.write_text("Use Spanish.")
        self.args = [
            "check",
            "--constraints",
            str(self.constraints),
            "--context",
            str(self.context),
            "--format",
            "json",
        ]

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, extra=(), judge=None):
        output = io.StringIO()
        with (
            patch("compaction_check.cli.JevClient", return_value=judge or FakeJudge()),
            contextlib.redirect_stdout(output),
        ):
            code = main(self.args + list(extra))
        return code, json.loads(output.getvalue())

    def test_success_and_json_artifact(self):
        code, report = self.run_cli(["--output", str(self.output)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.output.read_text()), report)

    def test_regression_exits_one(self):
        code, report = self.run_cli(judge=FakeJudge(status="contradicted"))
        self.assertEqual(code, 1)
        self.assertTrue(report["complete"])

    def test_api_failure_exits_two_and_overwrites_old_pass_artifact(self):
        self.output.write_text('{"passed":true}')
        judge = Mock(model="fixture")
        judge.evaluate.side_effect = ProviderError("API unavailable.")
        code, report = self.run_cli(["--output", str(self.output)], judge)
        self.assertEqual(code, 2)
        self.assertFalse(report["complete"])
        self.assertFalse(json.loads(self.output.read_text())["passed"])

    def test_report_cannot_overwrite_input(self):
        before = self.context.read_bytes()
        code, _ = self.run_cli(["--output", str(self.context)])
        self.assertEqual(code, 2)
        self.assertEqual(self.context.read_bytes(), before)

    def test_missing_key_is_actionable(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
            code = main(self.args)
        self.assertEqual(code, 2)
        self.assertIn("TYPESAFE_API_KEY", json.loads(output.getvalue())["error"])

    def test_empty_context_does_not_require_key(self):
        self.context.write_text("")
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
            code = main(self.args)
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(output.getvalue())["complete"])


if __name__ == "__main__":
    unittest.main()
