"""Prove whitespace anchoring and per-claim review isolation through the real CLI.

Only external HTTP/DNS/provider replies are fixtures. Nodes, saved source parsing,
model schema parsing, validation, adjudication and artifact writes stay real.
"""
import copy
import json
import unittest
from tests.claim_review import test_review_and_discovery_integration as flow_tests
from tests.extraction.extraction_test_fixtures import proposed_batch


class QuoteValidationIntegrationTests(unittest.TestCase):
    """Exercise the production route rather than a separate demonstration validator."""

    def setUp(self):
        """Reuse the existing offline transport harness with a deliberately wrapped source."""
        self.flow = flow_tests.ReviewDiscoveryIntegrationTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.flow.routes[("example.org", "/research")] = (200, {"Content-Type": "text/html"},
            b"<main>Alice works at <a href='/team'>Lab X.</a> Alice studies adversarial attacks.</main>")

    def test_original_spans_survive_extraction_review_and_adjudication(self):
        """A model's spaces can match page layout while evidence keeps exact saved newlines."""
        def review(body):
            """Return the usual claim with layout-normalized wording, not altered facts."""
            return self.flow.cli.response(text=json.dumps({"assessments": [{
                "candidate_id": "S1_P1_C1", "verdict": "supported", "in_scope": True,
                "rationale": "The passage states the affiliation.", "evidence_quotes": ["Alice works at Lab X."]}]}))
        self.flow.overrides["ClaimReviewBatch"] = review
        code, _, err, directory, session = self.flow.run_research(follow=False)
        self.assertEqual(code, 0, err)
        source = (directory / "documents/S1/text.txt").read_text()
        candidate = session["extraction"]["candidates"][0]
        self.assertEqual(candidate["adjudication_status"], "supported")
        self.assertEqual(candidate["publication_status"], "not_published")
        self.assertEqual(candidate["proposal"]["evidence"][0]["quote"], "Alice works at Lab X.")
        for anchor in [candidate["evidence"][0], session["claim_reviews"]["S1_P1_C1"]["evidence"][0]]:
            self.assertEqual(anchor["quote"], source[anchor["start"]:anchor["end"]])
            self.assertIn("\n", anchor["quote"])
            self.assertEqual(anchor["match_method"], "whitespace_normalized")
        self.assertEqual(session["usage"]["requests_attempted"], 3)

    def test_bad_quote_does_not_discard_neighboring_valid_assessment(self):
        """One fabricated quote leaves its claim unresolved, not every claim in the batch."""
        batch = proposed_batch()
        other = copy.deepcopy(batch["claims"][0])
        other.update(claim_id="C2", statement="Alice studies adversarial attacks.", relationship=None)
        other["evidence"] = [{"passage_id": "P1", "quote": "Alice studies adversarial attacks."}]
        batch["claims"].append(other)
        self.flow.overrides["ExtractionBatch"] = lambda body: self.flow.cli.response(text=json.dumps(batch))
        def review(body):
            """Pair one valid review with one invented source quotation."""
            return self.flow.cli.response(text=json.dumps({"assessments": [
                {"candidate_id": "S1_P1_C1", "verdict": "supported", "in_scope": True,
                 "rationale": "Affiliation is stated.", "evidence_quotes": ["Alice works at Lab X."]},
                {"candidate_id": "S1_P1_C2", "verdict": "supported", "in_scope": True,
                 "rationale": "Fault fixture.", "evidence_quotes": ["This quotation is fabricated."]}]}))
        self.flow.overrides["ClaimReviewBatch"] = review
        code, _, _, _, session = self.flow.run_research(follow=False)
        self.assertEqual(code, 1)
        self.assertEqual([c["adjudication_status"] for c in session["extraction"]["candidates"]],
            ["supported", "needs_review"])
        self.assertIsNone(session["claim_reviews"]["S1_P1_C1"]["error_code"])
        self.assertEqual(session["claim_reviews"]["S1_P1_C2"]["error_code"], "review_quote_not_found")
        self.assertEqual(session["usage"]["requests_attempted"], 3)
