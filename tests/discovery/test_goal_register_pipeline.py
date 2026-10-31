"""Check that per-goal claim history survives partial coverage assessments.

The command-line workflow extracts and reviews synthetic evidence and saves the
cumulative goal register. Tests compare those saved counts and statuses with what
later model requests receive, including assessments that consider only part of
the evidence. Claim counts must not become distinct-person counts or completed
research goals. No live model supplies the fixture judgments.
"""
import json
import unittest

from projectclarity.coverage.goal_register import refresh_goal_register
from projectclarity.research_schemas import ResearchSession
from tests.discovery import test_search_coverage_pipeline as search_fixtures


class GoalRegisterPipelineTests(unittest.TestCase):
    """Coverage slices must not replace or certify the cumulative goal-fact ledger."""

    def test_partial_assessment_keeps_prior_facts_and_status_changes(self):
        """Offer different findings across rounds while preserving every discovered claim.

        Args:
            self: Production terminal interface test with only external transports substituted.

        Returns:
            None. Assert separate latest-assessment and cumulative counts, persisted
            evidence references, multi-goal associations and explicit status revision.
        """
        fixture = search_fixtures.SearchCoveragePipelineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        seen = []

        def assess(body):
            """Return only the newest source's item rather than a cumulative list.

            Args:
                body: Actual serialized provider request from the production coverage node.

            Returns:
                Fixture provider response with valid citations and a first-round search.
            """
            payload = json.loads(body['messages'][0]['content'])
            seen.append(payload)
            first = len(seen) == 1
            cid = 'S1_P1_C1' if first else 'S2_P1_C1'
            answer = {'assessments': [{'criterion_id': 'K1', 'judgment': 'partial',
                'items': [{'key': 'Current offered item', 'candidate_ids': [cid]}],
                'target_count': None, 'benchmark_candidate_ids': [], 'evidence_candidate_ids': [cid],
                'gaps': ['Further verification needed.'], 'rationale': 'Only this evidence slice assessed.'}],
                'next_searches': [{'query': 'additional collection evidence', 'goal_ids': ['G1'],
                    'reason': 'Find complementary records.'}] if first else []}
            return fixture.cli.response(text=json.dumps(answer))

        fixture.overrides['CoverageAssessment'] = assess
        code, out, err, directory, saved = fixture.run_pipeline('--question', 'Which collections are documented?')
        self.assertEqual(code, 0, err)
        register = json.loads((directory / 'goal_register.json').read_text())
        self.assertEqual(register['total_facts'], 2)
        self.assertEqual(set(register['facts']), {'S1_P1_C1', 'S2_P1_C1'})
        self.assertEqual(saved['coverage']['criteria_progress'][0]['observed_count'], 1)
        self.assertEqual(seen[-1]['candidate_total'], 2)
        self.assertIn('not cumulative discovery', out)
        self.assertEqual(saved['request']['knowledge_max_requests'], 0)
        self.assertNotIn('accumulate_knowledge', saved['visited_nodes'])
        session = ResearchSession.model_validate(saved)
        candidate = session.extraction['candidates'][0]
        candidate['proposal']['goal_ids'] = ['G1', 'G2']
        candidate['adjudication_status'] = 'insufficient_evidence'
        refresh_goal_register(session, fixture.cli.output)
        changed = session.coverage['goal_register']
        self.assertEqual(changed['total_facts'], 2)
        self.assertEqual(changed['facts']['S1_P1_C1']['adjudication_status'], 'insufficient_evidence')
        self.assertTrue(changed['facts']['S1_P1_C1']['revisions'])
        self.assertTrue(all('S1_P1_C1' in g['candidate_ids'] for g in changed['goals']))
        session.extraction['candidates'] = session.extraction['candidates'][1:]
        refresh_goal_register(session, fixture.cli.output)
        self.assertEqual(changed['facts']['S1_P1_C1']['availability'], 'retained_not_in_current_snapshot')
        self.assertEqual(changed['total_facts'], 2)
