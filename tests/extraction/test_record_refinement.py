"""Check when missing extraction structure queues a focused reread of source text.

Handwritten two-record replies exercise missing relationships, unquoted records,
context-only results and one-level follow-up limits. The helper returns child
attempts with exact source offsets; these tests do not execute model rereads or
prove that a live repair will recover the missing information.
"""
import hashlib
import unittest

from projectclarity.extraction.extraction_contracts import ExtractionAttempt, ExtractionBatch
from projectclarity.extraction.record_refinement import record_followups


class RecordRefinementTests(unittest.TestCase):
    """Use non-Parliament records to check scope, stable IDs and restart accounting."""

    def attempt(self, *, context=False):
        """Construct a completed two-record reply with one unstructured association.

        Args:
            self: Test instance.
            context: Whether the finding is context-only rather than answer evidence.

        Returns:
            Source text and a typed completed attempt. No model or filesystem is used.
        """
        text = '[Record T1R2]\nAster | North\n\n[Record T1R3]\nBirch | South\n'
        batch = ExtractionBatch.model_validate({'entities': [
            {'entity_id': 'E1', 'name': 'Aster', 'entity_type': 'person'},
            {'entity_id': 'E2', 'name': 'North', 'entity_type': 'place'}], 'claims': [{
                'claim_id': 'C1', 'statement': 'Aster represents North.', 'goal_ids': ['G1'],
                'entity_ids': ['E1', 'E2'], 'relationship': None, 'qualifications': [],
                'research_roles': ['context'] if context else ['answer_evidence'],
                'evidence': [{'passage_id': 'P1', 'quote': 'Aster | North'}]}]})
        attempt = ExtractionAttempt(batch_id='S1_P1', source_id='S1', passage_id='P1',
            text_sha256=hashlib.sha256(text.encode()).hexdigest(), start=0, end=len(text),
            status='completed', result=batch)
        return text, attempt

    def test_missing_edges_and_unquoted_records_get_only_one_level(self):
        """Retain parent findings and deterministic, exact child source slices.

        Args:
            self: Test instance supplying assertions.

        Returns:
            None; fails on duplicate identities, invented facts or recursive follow-ups.
        """
        text, attempt = self.attempt()
        before = attempt.result.model_dump()
        children = record_followups(attempt, text)
        self.assertEqual(len(children), 2)
        self.assertEqual(attempt.unstructured_record_ids, ['T1R2'])
        self.assertEqual(attempt.unquoted_record_ids, ['T1R3'])
        self.assertEqual(attempt.result.model_dump(), before)
        self.assertEqual([c.batch_id for c in children], [c.batch_id for c in record_followups(attempt, text)])
        for child in children:
            self.assertEqual(child.refinement_parent, attempt.batch_id)
            self.assertTrue(text[child.start:child.end].startswith('[Record '))
            child.status = 'completed'
            child.result = attempt.result
            self.assertEqual(record_followups(child, text), [])

    def test_context_only_batches_are_accounted_without_extra_calls(self):
        """Do not turn footer/history context into automatic extraction work.

        Args:
            self: Test instance supplying assertions.

        Returns:
            None; fails if context-only findings schedule refinements.
        """
        text, attempt = self.attempt(context=True)
        self.assertEqual(record_followups(attempt, text), [])
        self.assertEqual(attempt.unquoted_record_ids, ['T1R3'])

    def test_empty_or_single_record_reply_never_retries_itself(self):
        """A record-level attempt has no unbounded retry path, even with no findings.

        Args:
            self: Test instance supplying assertions.

        Returns:
            None; fails if an empty or single-record result generates another call.
        """
        text, attempt = self.attempt()
        attempt.end = text.index('[Record T1R3]')
        self.assertEqual(record_followups(attempt, text), [])
        attempt.end = len(text)
        attempt.result = ExtractionBatch(entities=[], claims=[])
        self.assertEqual(record_followups(attempt, text), [])
