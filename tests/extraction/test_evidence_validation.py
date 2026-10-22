"""Check the Python boundary between a plausible assertion and inspectable evidence.

These tests never call an LLM. They deliberately propose quotes and references
that look reasonable but are missing, duplicated or point to the wrong passage.
They also demonstrate the limit: exact matching cannot decide whether a real
quotation logically supports a claim. That future semantic reviewer is separate.
"""

import unittest

from pydantic import ValidationError

from projectclarity.extraction.extraction_contracts import ExtractionAttempt, ExtractionBatch
from projectclarity.extraction.validate_evidence_node import validate_batch
from tests.extraction.extraction_test_fixtures import SOURCE_TEXT, proposed_batch


class EvidenceValidationTests(unittest.TestCase):
    """Exercise reference/anchoring policy without replacing the actual validator."""

    def validate(self, batch, text=SOURCE_TEXT, start=0):
        """Build an application-owned receipt around the model-shaped proposal."""
        attempt = ExtractionAttempt(batch_id="S1_P1", source_id="S1", text_sha256="fixturehash",
            passage_id="P1", start=start, end=len(text), status="completed",
            result=ExtractionBatch.model_validate(batch))
        return validate_batch(attempt, text, {"G1"})

    def test_unicode_offsets_locate_the_exact_saved_text_without_approval(self):
        """Offsets are characters, not UTF-8 bytes; anchoring never approves publication."""
        text = "évidence: " + SOURCE_TEXT
        result = self.validate(proposed_batch(), text, len("évidence: "))[0]
        evidence = result.evidence[0]
        self.assertEqual(text[evidence.start:evidence.end], evidence.quote)
        self.assertEqual(result.evidence_status, "anchored")
        self.assertEqual(result.review_status, "unreviewed")
        self.assertEqual(result.publication_status, "not_published")

    def test_bad_goals_entities_passages_and_quotes_remain_invalid_candidates(self):
        """Every broken reference is visible, rather than silently repaired or accepted."""
        for field, expected in [("goal", "unknown_goal_id"), ("entity", "unknown_entity_id"),
                                ("passage", "unknown_passage_id"), ("quote", "quote_not_found")]:
            with self.subTest(field=field):
                batch = proposed_batch()
                claim = batch["claims"][0]
                if field == "goal":
                    claim["goal_ids"] = ["G999"]
                elif field == "entity":
                    claim["entity_ids"].append("E999")
                elif field == "passage":
                    claim["evidence"][0]["passage_id"] = "P999"
                else:
                    claim["evidence"][0]["quote"] = "This sentence was never in the document."
                result = self.validate(batch)[0]
                self.assertEqual(result.evidence_status, "invalid")
                self.assertIn(expected, result.validation_errors)
                self.assertEqual(result.proposal.statement, claim["statement"])

    def test_ambiguous_and_duplicate_ids_are_not_guessed_or_overwritten(self):
        """Repeated quotations and model IDs need review, not an arbitrary match."""
        batch = proposed_batch()
        batch["claims"].append(batch["claims"][0].copy())
        batch["entities"].append(batch["entities"][0].copy())
        results = self.validate(batch, SOURCE_TEXT + SOURCE_TEXT)
        self.assertEqual(len({item.candidate_id for item in results}), 2)
        for item in results:
            self.assertEqual(set(item.validation_errors), {"ambiguous_quote", "duplicate_claim_id", "duplicate_entity_id"})
            self.assertEqual(item.evidence, [])

    def test_relationship_endpoints_must_be_declared_and_linked_to_the_claim(self):
        """An otherwise valid quotation cannot excuse a fabricated relationship endpoint."""
        batch = proposed_batch()
        batch["claims"][0]["relationship"]["object_id"] = "E999"
        result = self.validate(batch)[0]
        self.assertIn("unknown_entity_id", result.validation_errors)
        self.assertIn("relationship_not_in_claim_entities", result.validation_errors)

    def test_real_quote_does_not_prove_semantic_support(self):
        """A visited-lab quote can anchor a works-for claim but cannot validate its meaning."""
        batch = proposed_batch()
        batch["claims"][0]["evidence"][0]["quote"] = "Alice visited Lab X."
        result = self.validate(batch, "Alice visited Lab X.")[0]
        self.assertEqual(result.evidence_status, "anchored")
        self.assertEqual(result.review_status, "unreviewed")

    def test_model_cannot_set_publication_status_or_return_wrong_field_shapes(self):
        """Only the application owns acceptance controls, even in well-formed JSON."""
        for change in ("publication", "missing_evidence"):
            with self.subTest(change=change):
                batch = proposed_batch()
                if change == "publication":
                    batch["claims"][0]["publication_status"] = "published"
                else:
                    batch["claims"][0]["evidence"] = []
                with self.assertRaises(ValidationError):
                    ExtractionBatch.model_validate(batch)
