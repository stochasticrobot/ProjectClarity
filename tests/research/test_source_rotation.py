"""Check when recent reading gains justify continuing or switching documents.

Constructed passage histories distinguish new items and support upgrades from
repeated mentions, failed reads and unfinished validation. Tests retain switching
state across serialization and inspect the compact known-source context offered
to selection tasks. No model or network request is made.
"""
import copy
import unittest
from types import SimpleNamespace

from projectclarity.research_schemas import Goal, ResearchRequest, URLSource
from projectclarity.nodes.schedule_work.source_yield import document_yields, passage_gains
from projectclarity.nodes.schedule_work.source_rotation import rotation_work, selected_source_work
from projectclarity.discovery.registered_source_context import registered_source_context, add_registered_context


class SourceRotationTests(unittest.TestCase):
    """Keep scheduling observations distinct from evidence approval and completion."""

    def setUp(self):
        """Create a shaped goal, one useful passage and three duplicate passages.

        Args:
            self: Test owning isolated mutable in-memory receipts.
        Returns:
            None; no transports or evidence validators are replaced.
        """
        self.session = SimpleNamespace(request=ResearchRequest(question='List members', plan_only=False,
            extract_knowledge=True, review_claims=True, max_no_gain_passages=3, budget={'max_requests': 30}),
            usage=SimpleNamespace(requests_attempted=10), search={}, processing_calls=[],
            plan=SimpleNamespace(goals=[Goal(goal_id='G1', question='List members',
                evidence_required=['Membership'], result_shape={'unit': 'members', 'required_fields': ['name']})]),
            sources=[URLSource(source_id='S1', url='https://example.org/list')],
            documents=[SimpleNamespace(source_id='S1', status='retrieved', text_sha256='hash',
                final_url='https://example.org/list')],
            workflow={'document_focus': {'source_id': 'S1', 'text_sha256': 'hash', 'status': 'reading'},
                'finalized_batches': [f'S1_P{i}' for i in range(1, 5)]},
            model_attempts=[{'task': 'extract_knowledge', 'call_id': f'S1_P{i}'} for i in range(1, 5)],
            extraction={'attempts': [], 'candidates': []})
        for i in range(1, 5):
            self.session.extraction['attempts'].append({'source_id': 'S1', 'text_sha256': 'hash',
                'batch_id': f'S1_P{i}', 'status': 'completed'})
            self.session.extraction['candidates'].append({'candidate_id': f'S1_P{i}_C1',
                'batch_id': f'S1_P{i}', 'source_id': 'S1', 'text_sha256': 'hash',
                'evidence_status': 'anchored', 'evidence': [{'quote': 'A is a member.'}],
                'adjudication_status': 'pending', 'proposal': {'statement': f'Mention {i} of A.',
                    'claim_kind': 'non_relational', 'goal_ids': ['G1'], 'research_roles': ['answer_evidence'],
                    'result_records': [{'goal_id': 'G1', 'fields': [{'name': 'name', 'value': 'A'}]}]}})

    def test_repeated_item_claims_do_not_reset_gain_but_support_upgrade_does(self):
        """Separate target novelty from duplicate mentions and subsequent support.

        Args:
            self: Four different statements identifying the same target.
        Returns:
            None; duplicate passages are dry until the last one gains actual support.
        """
        self.assertEqual(document_yields(self.session)['S1']['no_gain_passages'], 3)
        self.session.extraction['candidates'][-1]['adjudication_status'] = 'supported'
        self.assertEqual(document_yields(self.session)['S1']['no_gain_passages'], 0)

    def test_counterevidence_and_unshaped_goals_preserve_useful_progress(self):
        """Keep negative findings and non-enumeration tasks eligible for reading.

        Args:
            self: Shaped fixture varied to include contrary evidence then an explanation goal.
        Returns:
            None; both meaningful alternatives yield gain without fabricated records.
        """
        last = self.session.extraction['candidates'][-1]['proposal']
        last.update(research_roles=['counterevidence'], result_records=[])
        self.assertGreater(passage_gains(self.session)['S1_P4'], 0)
        self.session.plan.goals[0].result_shape = None
        last['research_roles'] = ['answer_evidence']
        self.assertGreater(passage_gains(self.session)['S1_P4'], 0)

    def test_failures_and_unfinalized_passages_are_not_zero_gain(self):
        """Do not use failed model work or pending validation as evidence of exhaustion.

        Args:
            self: Completed duplicate fixture whose final observation becomes uncertain.
        Returns:
            None; each uncertain condition breaks the successful dry tail.
        """
        self.session.extraction['attempts'][-1]['status'] = 'failed'
        self.assertEqual(document_yields(self.session)['S1']['no_gain_passages'], 0)
        self.session.extraction['attempts'][-1]['status'] = 'completed'
        self.session.workflow['finalized_batches'].pop()
        self.assertEqual(document_yields(self.session)['S1']['no_gain_passages'], 0)

    def test_tail_follows_execution_not_document_inventory_order(self):
        """Account for section probes and later refinements in actual reservation order.

        Args:
            self: Fixture with an inventory shuffled relative to its wire receipts.
        Returns:
            None; input inventory shuffling cannot hide the three-pass plateau.
        """
        self.session.extraction['attempts'].reverse()
        self.assertEqual(document_yields(self.session)['S1']['no_gain_passages'], 3)

    def test_rotation_survives_restart_and_retains_queue_without_repeated_search(self):
        """Persist the pause and finish one search opportunity without resetting budgets.

        Args:
            self: Plateaued source with a runnable search and an unread passage.
        Returns:
            None; restart retains pause, search closes rotation, queued work is untouched.
        """
        pending = {'source_id': 'S1', 'text_sha256': 'hash', 'batch_id': 'S1_P5', 'status': 'not_attempted'}
        self.session.extraction['attempts'].append(pending)
        tasks = [{'action': 'web_search'}]
        self.assertEqual(rotation_work(self.session, tasks), tasks)
        restored = copy.deepcopy(self.session)
        self.assertEqual(rotation_work(restored, tasks), tasks)
        restored.search['queries'] = [{'query': 'another source'}]
        self.assertIsNone(rotation_work(restored, tasks))
        self.assertEqual(restored.workflow['yield_resets']['S1']['attempted'], 4)
        self.assertEqual(restored.extraction['attempts'][-1], pending)
        self.assertEqual(restored.usage.requests_attempted, 10)
        self.assertEqual(document_yields(restored)['S1']['no_gain_passages'], 0)

    def test_selected_official_gets_handoff_before_another_search(self):
        """Require a source-owned first-read task without bypassing blocked retrieval.

        Args:
            self: Run with one unattempted search-selected official source.
        Returns:
            None; ready handoff beats search, but no-ready-source leaves search usable.
        """
        self.session.sources.append(URLSource(source_id='S2', url='https://ourcommons.ca/Members',
            discovered_from_search='R1'))
        task = {'action': 'retrieve_sources', 'source_id': 'S2'}
        self.assertEqual(selected_source_work(self.session, [task, {'action': 'web_search'}]), [task])
        self.assertIsNone(selected_source_work(self.session, [{'action': 'web_search'}]))

    def test_registered_source_examples_preserve_pending_status_and_source_local_counts(self):
        """Offer useful selector context without promoting pending claims or other runs.

        Args:
            self: Four duplicate pending records in one source and one unread URL.
        Returns:
            None; context reports one pending tuple, no support and no unread contents.
        """
        self.session.sources.append(URLSource(source_id='S2', url='https://ourcommons.ca/Members'))
        rows = registered_source_context(self.session)
        self.assertEqual(rows[0]['goals'][0]['pending'], 1)
        self.assertEqual(rows[0]['goals'][0]['supported'], 0)
        self.assertEqual(rows[0]['goals'][0]['examples'][0]['status'], 'pending')
        self.assertEqual(rows[1]['goals'], [])
        self.assertFalse(rows[1]['passage_inventory_available'])

    def test_large_registered_inventory_keeps_urls_and_bounds_summary_input(self):
        """Avoid turning source summaries into an oversized model input.

        Args:
            self: One populated source expanded with 249 unread URL references.
        Returns:
            None; all URLs survive, detailed omissions are disclosed and serialized
            input remains within the provider's actual nested-JSON limit.
        """
        import json
        self.session.sources.extend(URLSource(source_id=f'S{i}', url=f'https://example.org/page/{i}')
            for i in range(2, 251))
        payload = {'original_question': 'x' * 25000}
        add_registered_context(self.session, payload, 'Unchanged user prompt')
        self.assertEqual(len(payload['registered_sources']), 250)
        self.assertGreater(payload['registered_source_summaries_omitted'], 0)
        serialized = json.dumps([('system', 'Unchanged user prompt'),
            ('human', json.dumps(payload, ensure_ascii=False))], ensure_ascii=False)
        self.assertLessEqual(len(serialized), 48000)

    def test_second_host_support_counts_but_repeated_same_host_support_does_not(self):
        """Reward a new support source without making repeated mentions look novel.

        Args:
            self: One repeated target across two independently retrieved hostnames.
        Returns:
            None; another host earns a scheduling signal, not a truth upgrade or
            an assertion that the two publishers are independent.
        """
        for c in self.session.extraction['candidates']:
            c['adjudication_status'] = 'supported'
        self.assertEqual(document_yields(self.session)['S1']['no_gain_passages'], 3)
        self.session.sources.append(URLSource(source_id='S2', url='https://ourcommons.ca/list'))
        self.session.documents.append(SimpleNamespace(source_id='S2', status='retrieved',
            text_sha256='second', final_url='https://ourcommons.ca/list'))
        other = copy.deepcopy(self.session.extraction['candidates'][0])
        other.update(candidate_id='S2_P1_C1', batch_id='S2_P1', source_id='S2', text_sha256='second')
        self.session.extraction['candidates'].append(other)
        self.assertGreater(passage_gains(self.session)['S2_P1'], 0)

    def test_review_upgrade_reconsiders_pause_before_spending_on_search(self):
        """Cancel a stale dry-tail decision when its pending review adds useful support.

        Args:
            self: Plateau fixture whose final duplicate has not yet been reviewed.
        Returns:
            None; review runs first, then fresh support resumes focus without a search.
        """
        review = {'action': 'review_claims'}
        search = {'action': 'web_search'}
        self.assertEqual(rotation_work(self.session, [search, review]), [review])
        self.session.extraction['candidates'][-1]['adjudication_status'] = 'supported'
        self.assertIsNone(rotation_work(self.session, [search]))
        self.assertEqual(self.session.workflow['source_rotation']['outcome'], 'progress_recovered')

    def test_rejected_counterclaim_cannot_keep_document_focus_alive(self):
        """Distinguish useful contrary evidence from a proposal rejected by review.

        Args:
            self: Duplicate fixture with a new but contradicted counterclaim.
        Returns:
            None; rejection leaves the dry tail intact rather than awarding progress.
        """
        last = self.session.extraction['candidates'][-1]
        last['proposal'].update(research_roles=['counterevidence'], result_records=[])
        last['adjudication_status'] = 'contradicted'
        self.assertEqual(document_yields(self.session)['S1']['no_gain_passages'], 3)
