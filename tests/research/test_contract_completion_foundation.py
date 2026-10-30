"""Verify source context, no-target scheduling and bounded evidence bookkeeping.

Transport fixtures exercise terminal production paths; component tests separately
probe partition and cache invariants. No test here establishes live-model judgment
quality. Integrated batch-based completion is exercised separately in
test_contract_completion.py through the real terminal entry point.
"""
import copy
import json
import unittest

from projectclarity.coverage.evidence_batches import plan_evidence_batches, reusable_batch
from projectclarity.extraction.extraction_contracts import CandidateClaim
from projectclarity.research_schemas import RetrievedDocument, ResearchSession, FileSource
from projectclarity.source_context import source_context
from tests.research.research_test_fixtures import example_session
from tests.research.test_answer_schema import specification
from tests.research import test_result_reporting_cli as reporting
from tests.claim_review import test_batched_review_cli as reviewing


def candidate(index):
    """Create an explicit artificial evidence record for partitioning tests only.

    Args:
        index: Nonnegative integer distinguishing this claim and its quoted text.
    Returns:
        Typed source-anchored fixture, without asserting a file-integrity check or
        invoking production extraction. Real source validation is tested separately.
    """
    quote = f'Event {index} occurred.'
    return CandidateClaim(candidate_id=f'S1_P1_C{index}', batch_id='S1_P1',
        source_id='S1', text_sha256='a'*64, evidence_status='anchored',
        validation_errors=[], adjudication_status='supported',
        proposal=dict(claim_id=f'C{index}', statement=quote, goal_ids=['G1'],
            entity_ids=[], relationship=None, qualifications=[],
            evidence=[dict(passage_id='P1', quote=quote)]),
        evidence=[dict(source_id='S1', text_sha256='a'*64, passage_id='P1',
            start=0, end=len(quote), quote=quote)])


class ContractCompletionFoundationTests(unittest.TestCase):
    """Keep mechanics, production integration and semantic guarantees distinct."""

    def test_source_context_reaches_real_extraction_and_review(self):
        """Carry retrieved HTML metadata through real terminal execution.

        Args:
            self: Test owner using synthetic HTTP/model transport responses only.
        Returns:
            None; asserts acquisition/title/date context in real SDK requests and
            saved receipts, without equating the retrieved date with currentness.
        """
        fixture = reporting.ResultReportingCLITests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.partial = False
        run_cli = fixture.cli.run_cli

        def run(provider, *args, **kwargs):
            """Add source-stated metadata to the fixture before real acquisition.

            Args:
                provider: Existing synthetic model HTTP handler.
                args: Terminal flags selected by the fixture.
                kwargs: Existing harness configuration including DNS fixture.
            Returns:
                The production CLI result; replaces source bytes, not pipeline code.
            """
            status, headers, body = fixture.flow.routes[('example.org', '/research')]
            prefix = b'<html><head><title>Historical registry</title><meta name="publisher" content="Registry office"><meta property="article:published_time" content="2001-01-01"></head><body>'
            fixture.flow.routes[('example.org', '/research')] = (
                status, headers, prefix + body + b'</body></html>')
            return run_cli(provider, *args, **kwargs)

        fixture.cli.run_cli = run
        code, _, err, _, saved, _ = fixture.run_case()
        self.assertEqual(code, 0, err)
        document = saved['documents'][0]
        for payload in [*fixture.extraction_inputs, *fixture.review_inputs]:
            context = payload['source_context']
            self.assertEqual(context['url'], 'https://example.org/research')
            self.assertEqual(context['final_url'], document['final_url'])
            self.assertEqual(context['acquired_at'], document['acquired_at'])
            self.assertIsNotNone(context['acquired_at'])
            self.assertEqual(context['source_stated_metadata']['title'], 'Historical registry')
            self.assertIn(dict(name='article:published_time', value='2001-01-01'),
                context['source_stated_metadata']['declarations'])
        session = ResearchSession.model_validate(saved)
        from projectclarity.coverage.stopping import evidence_revision
        before = evidence_revision(session)
        session.documents[0].source_metadata['title'] = 'Different source label'
        self.assertNotEqual(before, evidence_revision(session))

    def test_multi_source_review_keeps_each_context_separate(self):
        """Prevent first-passage metadata from leaking onto other review passages.

        Args:
            self: Test owner running the real four-source terminal fixture.
        Returns:
            None; asserts each batched passage uses its own source URL and hash.
        """
        fixture = reviewing.BatchedReviewCLITests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        result = fixture.run_case()
        self.assertEqual(result[0], 0, result[2])
        payload = next(p for p in fixture.review_inputs if len(p.get('passages', [])) > 1)
        self.assertNotIn('source_context', payload)
        urls = []
        for passage in payload['passages']:
            context = passage['source_context']
            self.assertEqual(context['source_id'], passage['source_id'])
            self.assertTrue(context['text_sha256'])
            self.assertIsNotNone(context['acquired_at'])
            urls.append(context['url'])
        self.assertEqual(len(urls), len(set(urls)))

    def test_local_context_hides_absolute_paths_and_keeps_missing_metadata_unknown(self):
        """Expose useful provenance without sending the workstation's directory layout.

        Args:
            self: Test owner projecting a local source with no acquisition receipt.
        Returns:
            None; missing metadata stays unknown and only the basename is exposed.
        """
        session = example_session()
        session.sources = [FileSource(source_id='S1', path='/private/work/books/source.txt')]
        context = source_context(session, 'S1')
        self.assertEqual(context['filename'], 'source.txt')
        self.assertIsNone(context['url'])
        self.assertIsNone(context['acquired_at'])
        self.assertEqual(context['source_stated_metadata'], {})
        self.assertNotIn('/private', json.dumps(context))

    def test_batches_account_for_the_entire_population_and_reuse_only_exact_inputs(self):
        """Exercise more than one context and reject unsafe assessment-cache reuse.

        Args:
            self: Test owner using 193 synthetic evidence records, not live findings.
        Returns:
            None; proves complete partitioning, size bounds, stable full batches and
            invalidation on candidate, review, prompt, source or contract changes.
        """
        session = example_session()
        session.documents = [RetrievedDocument(source_id='S1', source_kind='url',
            original_location='https://example.org/research', attempted_at=session.created_at,
            acquired_at=session.created_at, text_sha256='a'*64, status='retrieved')]
        rows = [candidate(i) for i in range(193)]
        valid = {c.candidate_id: c for c in rows}
        context = dict(answer_schema=specification(), criteria=['C1'])

        def batches(current=valid, data=context, prompt='Synthetic partition fixture'):
            """Call the real partitioner with a deliberately small context allowance.

            Args:
                current: Ordered candidate mapping, optionally varied for this check.
                data: Contract snapshot, optionally replaced to test invalidation.
                prompt: Test-only text, never registered as an application prompt.
            Returns:
                Complete bounded descriptors; no model request is made.
            """
            return plan_evidence_batches(session, current, data, prompt,
                max_characters=15_000, max_candidates=8)

        original = batches()
        self.assertGreater(len(original), 2)
        self.assertEqual([cid for b in original for cid in b['candidate_ids']], list(valid))
        self.assertTrue(all(b['characters'] <= 15_000 for b in original))
        extra = candidate(193)
        appended = batches({**valid, extra.candidate_id: extra})
        self.assertEqual([b['batch_id'] for b in original[:-1]],
                         [b['batch_id'] for b in appended[:-1]])
        first = original[0]
        receipt = dict(status='completed', batch_id=first['batch_id'],
            covered_candidate_ids=first['candidate_ids'], assessment={'fixture': True})
        self.assertTrue(reusable_batch(first, receipt))
        for patch in [dict(status='started'), dict(status='failed'), dict(error_code='parse_error'),
                      dict(covered_candidate_ids=[]), dict(assessment=None)]:
            self.assertFalse(reusable_batch(first, {**receipt, **patch}))
        changed = copy.deepcopy(valid)
        changed[rows[0].candidate_id].proposal.statement = 'Changed assertion.'
        updated = batches(changed)
        self.assertFalse(reusable_batch(updated[0], receipt))
        self.assertEqual(original[1]['batch_id'], updated[1]['batch_id'])
        self.assertNotEqual(first['batch_id'], batches(data={'criteria': ['C2']})[0]['batch_id'])
        self.assertNotEqual(first['batch_id'], batches(prompt='Changed fixture prompt')[0]['batch_id'])
        session.claim_reviews[rows[0].candidate_id] = {'assessment': {'verdict': 'contradicted'}}
        self.assertNotEqual(first['batch_id'], batches()[0]['batch_id'])
        session.claim_reviews.clear()
        session.documents[0].acquired_at = '2030-01-01T00:00:00Z'
        self.assertNotEqual(first['batch_id'], batches()[0]['batch_id'])
        with self.assertRaisesRegex(ValueError, 'base_too_large'):
            plan_evidence_batches(session, valid, context, 'fixture', max_characters=1)
        with self.assertRaisesRegex(ValueError, 'item_too_large'):
            plan_evidence_batches(session, valid, {}, '', max_characters=200)

    def test_production_scheduler_dispatches_gap_search_without_a_target(self):
        """Route a saved contract blocker ahead of unrelated productive extraction.

        Args:
            self: Test owner using a scheduler journal fixture, not a model stub.
        Returns:
            None; the production scheduler reserves web_search, and its actual query
            projector selects the gap query. This does not prove search-provider yield.
        """
        from tests.research.test_workflow_control import WorkflowControlTests
        from projectclarity.nodes.web_search.node import proposed_queries
        from projectclarity.coverage.stopping import evidence_revision
        fixture = WorkflowControlTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        session = fixture.session
        session.answer_schema = specification((), (), format='narrative')
        session.request.budget = session.request.budget.model_copy(update={'max_requests': 50})
        session.request.extract_knowledge = True
        session.request.assess_coverage = True
        session.request.web_search = True
        fixture.document('S1')
        fixture.document('S2')
        session.extraction = dict(candidates=[candidate(1).model_dump(mode='json')],
            attempts=[dict(batch_id='S1_P1', source_id='S1', passage_id='P1',
                text_sha256='a'*64, start=0, end=100, status='completed'),
                dict(batch_id='S1_P2', source_id='S1', passage_id='P2',
                text_sha256='a'*64, start=100, end=200, status='not_attempted')])
        session.workflow['finalized_batches'] = ['S1_P1']
        query = 'source evidence resolving a chronology conflict'
        session.coverage.update(next_searches=[dict(query=query, goal_ids=['G1'],
            reason='Resolve the contract chronology blocker.')],
            history=[dict(evidence_revision=evidence_revision(session))])
        self.assertTrue(all(c.target_count is None for c in session.plan.completion_criteria))
        task = fixture.dispatch()
        self.assertEqual(task['action'], 'web_search')
        self.assertEqual(proposed_queries(fixture.session)[0]['query'], query)

    def test_gap_search_review_flush_is_bounded_and_persisted(self):
        """Keep a large review backlog from indefinitely postponing blocker searches.

        Args:
            self: Test owner supplying already-eligible scheduler tasks.
        Returns:
            None; two charged review calls yield to the search across serialization,
            while an unavailable search is never fabricated or dispatched.
        """
        from projectclarity.coverage.completion_work import contract_blocker_work
        session = example_session()
        session.extraction = dict(candidates=[candidate(1).model_dump(mode='json')])
        session.coverage['next_searches'] = [dict(query='resolve gap', goal_ids=['G1'])]
        tasks = [dict(action='review_claims'), dict(action='web_search'), dict(action='extract_knowledge')]
        self.assertEqual(contract_blocker_work(session, tasks)[0]['action'], 'review_claims')
        session.usage.requests_attempted += 2
        session = ResearchSession.model_validate_json(session.model_dump_json())
        self.assertEqual(contract_blocker_work(session, tasks)[0]['action'], 'web_search')
        self.assertEqual(contract_blocker_work(session, tasks[:1])[0]['action'], 'review_claims')

    def test_gap_queue_does_not_starve_the_sources_it_selected(self):
        """Give an accepted search lead a first reading before searching again.

        Args:
            self: Test owner supplying already-eligible acquisition and search tasks.
        Returns:
            None; the blocker policy hands off to the selected source and leaves
            queued queries intact. Blocked acquisition still permits independent work.
        """
        from projectclarity.coverage.completion_work import contract_blocker_work
        from projectclarity.research_schemas import URLSource
        session = example_session()
        session.request.budget = session.request.budget.model_copy(update={'max_requests': 20})
        session.request.plan_only = False
        session.request.extract_knowledge = True
        session.sources.append(URLSource(source_id='S2', url='https://example.org/gap',
            discovered_from_search='R1'))
        session.extraction = dict(candidates=[candidate(1).model_dump(mode='json')], attempts=[])
        session.coverage['next_searches'] = [dict(query='resolve gap', goal_ids=['G1'])]
        tasks = [dict(action='web_search'), dict(action='retrieve_sources', source_id='S2')]
        self.assertEqual(contract_blocker_work(session, tasks)[0]['action'], 'retrieve_sources')
        self.assertEqual(len(session.coverage['next_searches']), 1)
        self.assertEqual(contract_blocker_work(session, tasks[:1])[0]['action'], 'web_search')
