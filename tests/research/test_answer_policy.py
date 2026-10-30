"""Check the rules connecting supporting research and source choices to answer requirements.

Direct component tests reject unknown or missing requirement links, distinguish
explicit source rejection from an actionable choice, and keep leads separate from
answer entries. Pending answer evidence and unresolved contrary evidence must remain
visible. These controlled checks complement, not replace, full-workflow tests.
"""
import copy
import unittest

from projectclarity.answer_schema.binding import bind_report
from projectclarity.answer_schema.requirements import requirement_goals
from projectclarity.answer_schema.inventory import report_inventory
from projectclarity.discovery.selection_acceptance import accept_choices
from projectclarity.nodes.select_sources.node import ContractSearchChoice, SearchSourceChoice
from tests.research.test_contract_completion import explicit_specification
from tests.research.research_test_fixtures import example_session
from tests.research import test_result_accounting as accounting
from tests.research.test_answer_schema import specification


class AnswerPolicyTests(unittest.TestCase):
    """Reject scope laundering without relaxing legitimate unresolved answer gates."""

    def test_auxiliary_links_are_explicit_and_do_not_change_contract_units(self):
        """Allow declared supporting work, rejecting unmapped or invented obligations.

        Args:
            self: Test owner checking accepted plan/contract boundaries.
        Returns:
            None; mappings expand eligible evidence goals, never the requested units.
        """
        session = example_session()
        schema = explicit_specification(supporting=True)
        with self.assertRaisesRegex(ValueError, 'Auxiliary goal'):
            bind_report(session.plan, schema, require_mappings=True)
        session.plan.goals[1].requirement_ids = ['invented']
        with self.assertRaisesRegex(ValueError, 'unknown answer requirement'):
            bind_report(session.plan, schema, require_mappings=True)
        session.plan.goals[1].requirement_ids = ['A2']
        before = copy.deepcopy(schema)
        bind_report(session.plan, schema, require_mappings=True)
        self.assertEqual(schema, before)
        self.assertEqual(requirement_goals(session, session.plan.completion_criteria[0]), {'G1'})
        self.assertEqual(requirement_goals(session, session.plan.completion_criteria[1]), {'G1','G2'})
        self.assertEqual(session.plan.goals[1].result_shape, None)

    def test_disposition_is_an_action_boundary_not_list_membership(self):
        """Require both explicit action and compatible known requirement references.

        Args:
            self: Test owner providing structurally valid but adversarial choices.
        Returns:
            None; only one relevant, correctly mapped choice can be registered.
            Free-text rationale is not parsed as a hidden relevance classifier.
        """
        session = example_session()
        session.answer_schema = explicit_specification(supporting=True)
        session.plan.goals[1].requirement_ids = ['A2']
        bind_report(session.plan, session.answer_schema, require_mappings=True)
        specs = [('reject', [], ['G1']), ('specific_lead', ['A1'], ['G2']),
                 ('direct_evidence', ['unknown'], ['G1']), ('specific_lead', ['A2'], ['G2'])]
        choices = [ContractSearchChoice(result_id=f'R{i}', disposition=d, requirement_ids=r,
            goal_ids=g, reason='A structurally valid rationale.') for i,(d,r,g) in enumerate(specs)]
        offered = {f'R{i}':dict(url=f'https://example.net/source{i}') for i in range(4)}
        accepted, outcomes = accept_choices(session, choices, offered, 'result_id', 10)
        self.assertEqual([c.result_id for c in accepted], ['R3'])
        self.assertEqual([o['status'] for o in outcomes], ['declined','rejected','rejected','accepted'])
        self.assertEqual(outcomes[1]['reason_code'], 'requirement_goal_mismatch')
        from projectclarity.nodes.schedule_work.answer_controller import POLICY
        session.workflow['controller_policy'] = POLICY
        legacy = SearchSourceChoice(result_id='R0', goal_ids=['G1'], reason='Old caller cannot bypass dispositions.')
        accepted, outcomes = accept_choices(session, [legacy], offered, 'result_id', 1)
        self.assertEqual(accepted, [])
        self.assertEqual(outcomes[0]['reason_code'], 'missing_disposition')

    def test_leads_are_separate_but_pending_answers_and_counterevidence_remain(self):
        """Prevent answer-count inflation without hiding actual answer uncertainty.

        Args:
            self: Test owner using anchored candidate fixtures and real row projection.
        Returns:
            None; pure leads are excluded, pending answer fields stay pending and
            matching counterevidence blocks the row without itself proving contradiction.
        """
        fixture = accounting.ResultAccountingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        section = specification(('name','riding'), ('person_name','text'))['sections'][0]
        goal = copy.deepcopy(fixture.goal)
        goal['result_shape'] = dict(unit=section['unit'], required_fields=['name','riding'])
        answer = copy.deepcopy(fixture.candidate)
        answer['proposal']['result_records'][0]['fields'] = [dict(name='name',value='Rowan'), dict(name='riding',value='North')]
        answer['adjudication_status'] = 'pending'
        lead = copy.deepcopy(answer)
        lead['candidate_id'] = 'L'
        lead['proposal']['research_roles'] = ['lead']
        lead['proposal']['result_records'][0]['fields'][0]['value'] = 'Historical'
        rows = report_inventory(goal, [answer,lead], section, answer_scope_only=True)
        self.assertEqual((rows['identified'],rows['pending']), (1,1))
        answer['adjudication_status'] = 'supported'
        counter = copy.deepcopy(answer)
        counter['candidate_id'] = 'X'
        counter['proposal']['research_roles'] = ['counterevidence']
        rows = report_inventory(goal, [answer,counter,lead], section, answer_scope_only=True)
        self.assertEqual((rows['supported'],rows['pending']), (0,1))
        self.assertEqual(set(rows['records'][0]['candidate_ids']), {answer['candidate_id'],'X'})
        legacy = report_inventory(goal,[answer,lead],section)
        self.assertEqual(legacy['identified'],2)
