"""Check malformed UI responses before they reach a resumable workflow.

A web form and a CLI file are both untrusted input. These small tests exercise
the shared response contract and bounded JSON reader, without a model, graph,
or database. They complement the cross-process tests rather than replace them.
"""

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from projectclarity.plan_review_cli import read_decision_file
from projectclarity.plan_review_contracts import PlanReviewDecision
from projectclarity.research_planner import ResearchError


class ReviewResponseValidationTests(unittest.TestCase):
    """Reject contradictory choices, duplicate answers, and oversized transport input."""

    def test_invalid_response_shapes_are_not_coerced_into_approval(self):
        """A typo, duplicate answer, or budget override cannot quietly become an approval."""
        base = {"run_id": "run", "review_id": "review", "action": "accept_assumptions"}
        invalid = [dict(base, action="ignore_everything"), dict(base, max_http_requests=1000),
                   dict(base, schema_version="2.0"), dict(base, action="answer", answers=[
                       {"question_id": "Q1", "answer": "First"}, {"question_id": "Q1", "answer": "Second"}]),
                   dict(base, answers=[{"question_id": "Q1", "answer": "Contradictory"}]),
                   dict(base, action="answer", answers=[{"question_id": "Q1", "answer": 2026}]),
                   dict(base, action="answer", answers=[{"question_id": "Q1", "answer": "  "}])]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                PlanReviewDecision.model_validate(payload)

    def test_response_files_have_encoding_and_size_limits(self):
        """The CLI fails oversized or malformed files before constructing a decision."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "answers.json"
            for raw in (b"x" * 65537, b"\xff", b"not JSON"):
                with self.subTest(size=len(raw)):
                    path.write_bytes(raw)
                    with self.assertRaises(ResearchError):
                        read_decision_file(path)
            path.write_text(json.dumps({"Q1": "Only 2026."}))
            self.assertEqual(read_decision_file(path), {"Q1": "Only 2026."})
