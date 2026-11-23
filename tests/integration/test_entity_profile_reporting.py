"""Exercise report intent, profiles and recovery through real HTTP/graph entry points.

Only external model/search/download replies are fixtures. These tests establish
routing, persistence, evidence checks and rendering inputs, not live-model quality
or the semantic correctness of identity attribution.
"""
import copy
import csv
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from projectclarity.ux.web_app import create_app
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.reporting.contracts import saved_report_plan
from projectclarity.reporting.entity_profile.selection import validate_profile_batch
from projectclarity.reporting.entity_profile.assembly import assemble_profile
from tests.integration.test_document_pipeline import FixtureRun
from tests.integration.test_research_graph import FullWorkflowCaller, wait_for_completion
from tests.integration.test_batched_reporting import many_facts
from tests.integration.test_research_setup import DIRECTORY_PLAN

PROFILE_PLAN = {'report_type': 'entity_profile', 'reason': 'The user requests one named entity, not a list.',
                'subject': 'Acme', 'subject_kind': 'company', 'subject_scope': 'The AI tool developer in Canada.'}


def profile_reply(data):
    """Build cited source-bound fixture contributions for exactly the offered records.

    Args: data: Actual compact request from the production profile builder.
    Returns: Schema-shaped batch, with literal identity quotes and one requested
        section filled. Remaining sections stay unknown; no provider access occurs.
    """
    facts = data['facts']
    findings = [{'text': f['fact']['statement'], 'status': 'supported', 'fact_ids': [f['id']]} for f in facts]
    return {'identity_assessments': [{'fact_id': f['id'], 'status': 'match', 'reason': 'Fixture explicitly identifies the subject.',
              'identity_quote': data['evidence_passages'][f['evidence'][0]]['quote']} for f in facts],
            'overview': findings[:1], 'sections': [{'requirement_id': data['requirements'][0]['id'], 'findings': findings}],
            'unused_fact_ids': []}


class ProfileCaller(FullWorkflowCaller):
    """Capture actual stage inputs and return profile-shaped external responses."""
    def __init__(self, plan=None, questions=None, refined_plan=None, fail_ids=()):
        """Configure explicit intent and optional output failures without network access.

        Args: plan: Initial report choice; questions: Clarification fixture questions;
            refined_plan: Choice after answers, defaulting to initial choice;
            fail_ids: Report fact IDs whose batch returns invalid output on every attempt.
        Returns: None; initializes request history and deterministic fixture policy.
        """
        super().__init__(questions=[] if questions is None else questions)
        self.plan = copy.deepcopy(plan or PROFILE_PLAN)
        self.refined_plan = copy.deepcopy(refined_plan or self.plan)
        self.fail_ids = set(fail_ids)

    def __call__(self, stage, request, sid):
        """Substitute only model output while keeping planning and reporting code real.

        Args: stage: Actual workflow label; request: Unchanged production request;
            sid: Session identity retained in recorded calls.
        Returns: JSON fixture; configured report failures remain invalid for both
            attempts. Clarification and refinement preserve their real input binding.
        """
        if stage in ('clarify', 'refine'):
            output = json.loads(super().__call__(stage, request, sid))
            output['report_plan'] = self.plan if stage == 'clarify' else self.refined_plan
            if stage == 'refine':
                output['research_question'] = 'Profile Acme, the AI tool developer in Canada.'
            return json.dumps(output)
        if stage.startswith('report-') and 'identity_assessments' in request['response_schema']['properties']:
            self.calls.append((stage, request, sid))
            data = json.loads(request['messages'][0]['content'])
            if self.fail_ids & {f['id'] for f in data['facts']}:
                return '{}'
            return json.dumps(profile_reply(data))
        return super().__call__(stage, request, sid)

    def count_input_tokens(self, request):
        """Force real recursive report splitting above two records, offline only.

        Args: request: Complete request to inspect for fixture batch length.
        Returns: Synthetic count, not provider measurement; no request is sent.
        """
        data = json.loads(request['messages'][0]['content'])
        return 20000 if len(data.get('facts', [])) > 2 else 1000


class EntityProfileTests(unittest.TestCase):
    """Verify production intent selection, source attribution and retained gaps."""
    def test_full_http_workflow_selects_profile_before_search_and_survives_restart(self):
        """Run question through searches, extraction, review, profile and CSV.

        Args: None.
        Returns: None; proves real registry dispatch, cited profile and plan
            propagation, read-only refresh/restart and no directory-shaped response.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp); caller = ProfileCaller(); sid = str(uuid4())
            sessions = Sessions(root, caller, runner_factory=FixtureRun)
            try:
                with TestClient(create_app(sessions)) as client:
                    response = client.post('/api/setup', json={'session_id': sid, 'question': 'Profile Acme, the Canadian AI tool developer.',
                        'auto_advance': True, 'research_rounds': 1})
                    self.assertEqual(response.status_code, 202)
                    state = wait_for_completion(client, sid)
                    self.assertEqual(state['status'], 'report_completed', state)
                    self.assertEqual(state['report_plan'], PROFILE_PLAN)
                    for stage, request, _ in caller.calls:
                        if stage == 'search_queries' or stage.startswith('report-'):
                            self.assertEqual(json.loads(request['messages'][0]['content'])['report_plan'], PROFILE_PLAN)
                    report = client.get(f'/api/setup/{sid}/report-data').json()
                    self.assertEqual(report['presentation'], 'entity_profile')
                    self.assertEqual(report['entries'], [])
                    self.assertEqual(report['profile']['subject'], 'Acme')
                    self.assertEqual(report['summary'][0]['fact_ids'], ['F1'])
                    self.assertEqual(report['profile']['sections'][0]['findings'][0]['text'], 'Acme builds AI tools in Canada.')
                    csv_text = client.get(f'/api/setup/{sid}/report.csv').text
                    self.assertIn('Overview', csv_text); self.assertIn('https://example.org/doc', csv_text)
                    self.assertEqual(client.get('/assets/entity_profile_report.js').status_code, 200)
                    calls = len(caller.calls)
                    client.get(f'/report/{sid}'); client.get(f'/api/setup/{sid}/report-data')
                    self.assertEqual(len(caller.calls), calls)
            finally:
                sessions.close()
            replacement = ProfileCaller()
            sessions = Sessions(root, replacement)
            try:
                with TestClient(create_app(sessions)) as client:
                    self.assertEqual(client.get(f'/api/setup/{sid}').json()['report_plan'], PROFILE_PLAN)
                    self.assertEqual(client.get(f'/api/setup/{sid}/report-data').json(), report)
                    self.assertEqual(replacement.calls, [])
            finally:
                sessions.close()

    def test_clarification_can_change_type_and_future_types_do_not_launch_research(self):
        """Bind final format to user answers and reject unsupported formats explicitly.

        Args: None.
        Returns: None; directory-to-profile refinement persists without extra planning
            calls; unavailable formats retain intent and never search or fall back.
        """
        for future in (False, True):
            with TemporaryDirectory() as tmp:
                plan = {**DIRECTORY_PLAN, 'report_type': 'timeline'} if future else DIRECTORY_PLAN
                caller = ProfileCaller(plan=plan, questions=[] if future else ['One named company or a list of companies?'],
                                       refined_plan=PROFILE_PLAN)
                sessions = Sessions(Path(tmp), caller); sid = str(uuid4())
                try:
                    with TestClient(create_app(sessions)) as client:
                        client.post('/api/setup', json={'session_id': sid, 'question': 'Acme research', 'auto_advance': future})
                        state = wait_for_completion(client, sid)
                        if future:
                            self.assertEqual(state['status'], 'failed')
                            self.assertIn('not implemented', state['error'])
                            self.assertEqual(state['report_plan']['report_type'], 'timeline')
                            self.assertEqual([c[0] for c in caller.calls], ['define', 'clarify'])
                        else:
                            self.assertEqual(state['status'], 'clarification')
                            client.post(f'/api/setup/{sid}/answers', json={'answers': ['Only Acme, the Canadian AI developer.']})
                            state = wait_for_completion(client, sid)
                            self.assertEqual(state['status'], 'ready')
                            self.assertEqual(state['report_plan'], PROFILE_PLAN)
                            self.assertEqual([c[0] for c in caller.calls], ['define', 'clarify', 'refine'])
                            payload = json.loads(caller.calls[-1][1]['messages'][0]['content'])
                            self.assertEqual(payload['report_plan'], DIRECTORY_PLAN)
                            self.assertEqual(payload['answers'][0]['answer'], 'Only Acme, the Canadian AI developer.')
                finally:
                    sessions.close()

    def test_directory_population_metadata_survives_real_workflow(self):
        """Allow list target metadata without discarding scope or routing to profiles.

        Args: None.
        Returns: None; real HTTP workflow reaches directory report and CSV with
            population metadata preserved through searches and report requests.
        """
        plan = {**DIRECTORY_PLAN, 'subject': 'Canadian companies',
                'subject_kind': 'company', 'subject_scope': 'Companies doing AI work in Canada'}
        with TemporaryDirectory() as tmp:
            caller = ProfileCaller(plan=plan)
            sessions = Sessions(Path(tmp), caller, runner_factory=FixtureRun)
            sid = str(uuid4())
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post('/api/setup', json={'session_id': sid,
                        'question': 'What Canadian companies are doing AI work?',
                        'auto_advance': True, 'research_rounds': 1})
                    state = wait_for_completion(client, sid)
                    self.assertEqual(state['status'], 'report_completed', state)
                    self.assertEqual(state['report_plan'], plan)
                    report = client.get(f'/api/setup/{sid}/report-data').json()
                    self.assertEqual(report['presentation'], 'items')
                    self.assertNotIn('profile', report)
                    self.assertEqual(client.get(f'/api/setup/{sid}/report.csv').status_code, 200)
                    for stage, request, _ in caller.calls:
                        if stage == 'search_queries' or stage.startswith('report-'):
                            self.assertEqual(json.loads(request['messages'][0]['content'])['report_plan'], plan)
            finally:
                sessions.close()

    def test_failed_split_leaf_does_not_skip_siblings_and_retry_reuses_profile_tasks(self):
        """Exercise profile failures and saved retry via the real report HTTP route.

        Args: None.
        Returns: None; all eight facts offered despite one twice-invalid leaf,
            partial output/CSV retained, retry reuses successful batches and keeps
            the saved profile plan. No searches or other upstream calls occur.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp); state = many_facts(root, 8); state.pop('report_scheme')
            state['report_plan'] = PROFILE_PLAN; sid = state['session_id']
            caller = ProfileCaller(fail_ids=['F1']); sessions = Sessions(root, caller); sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post(f'/api/setup/{sid}/report')
                    state = wait_for_completion(client, sid)
                    self.assertEqual(state['status'], 'report_partial', state)
                    offered = {f['id'] for _, request, _ in caller.calls for f in json.loads(request['messages'][0]['content'])['facts']}
                    self.assertEqual(offered, {f'F{i}' for i in range(1, 9)})
                    report = client.get(f'/api/setup/{sid}/report-data').json()
                    self.assertEqual(report['coverage']['unreviewed_report'], 2)
                    self.assertIn('Partial report', client.get(f'/api/setup/{sid}/report.csv').text)
                    caller.fail_ids.clear(); before = len(caller.calls)
                    response = client.post(f'/api/setup/{sid}/report/retry', json={'previous_started_at': state['report']['started_at']})
                    self.assertEqual(response.status_code, 202)
                    state = wait_for_completion(client, sid)
                    self.assertEqual(state['status'], 'report_completed', state)
                    self.assertGreater(state['report']['reused_tasks'], 0)
                    self.assertEqual(len(caller.calls) - before, 1)
                    self.assertEqual(state['report_plan'], PROFILE_PLAN)
                    self.assertTrue(all(stage.startswith('report-') for stage, _, _ in caller.calls))
            finally:
                sessions.close()

    def test_namesakes_unknown_sections_conflicts_and_exact_identity_quotes(self):
        """Reject wrong identity attribution and keep explicit unknown/conflicting content.

        Args: None.
        Returns: None; validators reject invented IDs/quotes and ambiguous citations;
            distinct namesakes are withheld, all sections remain, and conflicts retain
            their citations. This tests enforcement, not semantic identity inference.
        """
        facts = [{'id': f'F{i}', 'evidence': [{'quote': quote}]} for i, quote in enumerate(
            ['Alex Lee is a Toronto physicist.', 'Alex Lee is an Ottawa poet.'], 1)]
        packet = {'report_plan': {**PROFILE_PLAN, 'subject': 'Alex Lee', 'subject_kind': 'person', 'subject_scope': 'Toronto physicist'},
                  'requirements': [{'id': 'R1', 'name': 'Research'}, {'id': 'R2', 'name': 'Education'}], 'facts': facts}
        raw = {'identity_assessments': [
            {'fact_id': 'F1', 'status': 'match', 'reason': 'Source identifies Toronto physicist.', 'identity_quote': facts[0]['evidence'][0]['quote']},
            {'fact_id': 'F2', 'status': 'ambiguous', 'reason': 'Poet may be a different person.', 'identity_quote': None}],
            'overview': [], 'sections': [{'requirement_id': 'R1', 'findings': [
                {'text': 'Conflicting descriptions remain unresolved.', 'status': 'conflicting', 'fact_ids': ['F1']}]}],
            'unused_fact_ids': []}
        checked = validate_profile_batch(json.dumps(raw), packet, facts)
        result = assemble_profile([checked], packet)
        self.assertEqual(result['profile']['sections'][1]['status'], 'unknown')
        self.assertEqual(result['profile']['sections'][0]['status'], 'conflicting')
        self.assertEqual(result['profile']['identity_uncertainty'][0]['fact_id'], 'F2')
        self.assertEqual(result['exclusions'][0]['fact_id'], 'F2')
        for ids in (['F2'], ['F999']):
            bad = copy.deepcopy(raw); bad['sections'][0]['findings'][0]['fact_ids'] = ids
            self.assertEqual(validate_profile_batch(json.dumps(bad), packet, facts)['sections'][0]['findings'], [])
        bad = copy.deepcopy(raw); bad['identity_assessments'][0]['identity_quote'] = 'Invented source quotation'
        self.assertEqual(validate_profile_batch(json.dumps(bad), packet, facts)['sections'][0]['findings'], [])
        bad = copy.deepcopy(raw); bad['identity_assessments'].pop(0); bad['sections'] = []
        self.assertEqual(validate_profile_batch(json.dumps(bad), packet, facts)['_recovery']['missing_fact_ids'], ['F1'])

    def test_partial_profile_recovers_only_missing_facts_through_http(self):
        """Preserve valid neighbours while recovering malformed fields via production.

        Args: None.
        Returns: None; HTTP report completes after only unresolved facts are
            reassessed, preserving the original valid contribution. No live I/O.
        """
        class PartialCaller(ProfileCaller):
            def __call__(self, stage, request, sid):
                """Supply mixed-validity first reply and valid targeted recovery.

                Args: stage: Call label; request: Production input; sid: Session ID.
                Returns: JSON fixture; records every production request.
                """
                text = super().__call__(stage, request, sid)
                if stage.startswith('report-') and len(self.calls) == 1:
                    raw = json.loads(text)
                    raw['identity_assessments'] = raw['identity_assessments'][:1]
                    raw['sections'][0]['findings'].append(
                        {'text': 'Not known', 'status': 'unknown', 'fact_ids': []})
                    raw['unused_fact_ids'] = ['F2']
                    return json.dumps(raw)
                return text
        with TemporaryDirectory() as tmp:
            root = Path(tmp); state = many_facts(root, 2); state.pop('report_scheme')
            state['report_plan'] = PROFILE_PLAN; sid = state['session_id']
            caller = PartialCaller(); sessions = Sessions(root, caller); sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post(f'/api/setup/{sid}/report')
                    state = wait_for_completion(client, sid)
                    self.assertEqual(state['status'], 'report_completed', state)
                    offered = [[f['id'] for f in json.loads(req['messages'][0]['content'])['facts']]
                               for _, req, _ in caller.calls]
                    self.assertEqual(offered, [['F1', 'F2'], ['F2']])
                    report = client.get(f'/api/setup/{sid}/report-data').json()
                    self.assertEqual(report['coverage']['unreviewed_report'], 0)
                    self.assertEqual({i for s in report['profile']['sections']
                                      for f in s['findings'] for i in f['fact_ids']}, {'F1', 'F2'})
            finally:
                sessions.close()

    def test_failed_targeted_recovery_retains_parent_and_honest_gap(self):
        """Keep usable profile findings when the unresolved-only retry also fails.

        Args: None.
        Returns: None; production reporting publishes one retained fact and one
            explicit gap, with bounded calls and no false exclusion.
        """
        class BrokenRecovery(ProfileCaller):
            def __call__(self, stage, request, sid):
                """Return a partial first batch and invalid recovery replies.

                Args: stage: Call label; request: Real request; sid: Session ID.
                Returns: Fixture JSON; all requests remain captured.
                """
                text = super().__call__(stage, request, sid)
                if stage.startswith('report-'):
                    if len(self.calls) > 1:
                        return '{}'
                    raw = json.loads(text)
                    raw['identity_assessments'] = raw['identity_assessments'][:1]
                    return json.dumps(raw)
                return text
        from threading import Event
        from projectclarity.reporting.report_generation import ReportRun
        with TemporaryDirectory() as tmp:
            root = Path(tmp); state = many_facts(root, 2); state.pop('report_scheme')
            state['report_plan'] = PROFILE_PLAN
            caller = BrokenRecovery()
            worker = ReportRun(root / state['session_id'], state, lambda _: None, caller, Event())
            worker.execute()
            report = json.loads((worker.root / 'report.json').read_text())
            self.assertEqual(report['coverage']['unreviewed_report_fact_ids'], ['F2'])
            self.assertEqual(report['exclusions'], [])
            self.assertEqual(len(caller.calls), 3)
            self.assertTrue(all([f['id'] for f in json.loads(req['messages'][0]['content'])['facts']] == ['F2']
                                for _, req, _ in caller.calls[1:]))
            self.assertEqual(report['summary'][0]['fact_ids'], ['F1'])

    def test_quarantine_does_not_accept_duplicate_ids_or_fabricated_quotes(self):
        """Keep strict evidence gates when neighbouring fields are recoverable.

        Args: None.
        Returns: None; duplicate assessments, invented quotes and invalid finding
            statuses stay unpublished and unresolved, even if labelled unused.
        """
        facts = [{'id': 'F1', 'evidence': [{'quote': 'Acme supplies tools.'}]}]
        packet = {'requirements': [{'id': 'R1'}]}
        assessment = {'fact_id': 'F1', 'status': 'match', 'reason': 'Named source.',
                      'identity_quote': 'Acme supplies tools.'}
        finding = {'text': 'Acme supplies tools.', 'status': 'supported', 'fact_ids': ['F1']}
        raw = {'identity_assessments': [assessment], 'overview': [finding],
               'sections': [], 'unused_fact_ids': []}
        for mode in ('duplicate', 'quote', 'status'):
            bad = copy.deepcopy(raw)
            if mode == 'duplicate':
                bad['identity_assessments'].append(assessment)
            elif mode == 'quote':
                bad['identity_assessments'][0]['identity_quote'] = 'Invented'
            else:
                bad['overview'][0]['status'] = 'unknown'
                bad['unused_fact_ids'] = ['F1']
            from projectclarity.reporting.entity_profile.recovery import inspect_profile
            result = inspect_profile(json.dumps(bad), packet, facts)
            self.assertEqual(result['summary'], [])
            self.assertEqual(result['exclusions'], [])
            self.assertEqual(result['_recovery']['missing_fact_ids'], ['F1'])
        with self.assertRaises(ValueError):
            validate_profile_batch('{"overview":[],"overview":[]}', packet, facts)

    def test_legacy_default_is_only_for_absent_plan_and_profile_csv_is_safe(self):
        """Preserve historical directory sessions without accepting malformed new plans.

        Args: None.
        Returns: None; explicit null fails, and profile exports retain gaps while
            protecting formula-like subject, finding, section and source cells.
        """
        from projectclarity.reporting.entity_profile.export import profile_csv
        self.assertEqual(saved_report_plan({})['report_type'], 'directory')
        with self.assertRaises(ValueError): saved_report_plan({'report_plan': None})
        report = {'profile': {'subject': '=SUM(1,2)', 'subject_scope': 'Requested scope',
                    'sections': [{'title': '=Section', 'findings': []}], 'identity_uncertainty': []},
                  'summary': [{'text': '=1+1', 'status': 'supported', 'fact_ids': ['F1']}],
                  'facts': [{'id': 'F1', 'evidence': [{'source_url': '=URL'}]}], 'coverage': {}, 'gaps': ['Unknown education']}
        rows = list(csv.reader(StringIO(profile_csv(report))))
        self.assertEqual(rows[1][0], "'=SUM(1,2)")
        self.assertEqual(rows[2][2], "'=1+1"); self.assertEqual(rows[2][5], "'=URL")
        self.assertEqual(rows[3][1], "'=Section"); self.assertEqual(rows[3][3], 'unknown')
        self.assertEqual(rows[4][2], 'Unknown education')

    def test_profile_through_bedrock_wire_modes_keeps_real_decoding_and_accounting(self):
        """Run profile reporting through both supported Nova wire representations.

        Args: None.
        Returns: None; only the AWS client and web responses are fixtures. Actual
            HTTP routing, SDK request translation, codec decoding, budgets, ledger,
            identity checks and profile assembly execute. No live AWS calls occur.
        """
        from projectclarity.model_access.providers.bedrock_provider import BedrockProvider
        from projectclarity.model_access.model_ledger import read_ledger
        from tests.integration.test_model_providers import ENV, nova_reply, fixture_wire_value
        for mode in ('json_text', 'tool_nullable'):
            with self.subTest(mode=mode), TemporaryDirectory() as tmp, patch.dict('os.environ', {**ENV, 'PROJECTCLARITY_BEDROCK_OUTPUT': mode}), patch.object(BedrockProvider, '_client') as factory:
                root = Path(tmp); sid = str(uuid4()); fixture = ProfileCaller()

                def send(**wire):
                    """Return fixture content conforming to the actually advertised wire schema.

                    Args: wire: Prepared production Converse request and input evidence.
                    Returns: Native AWS reply; schema translation/decoding stays real.
                    """
                    schema = (wire['toolConfig']['tools'][0]['toolSpec']['inputSchema']['json'] if mode == 'tool_nullable'
                              else json.loads(wire['system'][-1]['text'].split('Response schema:\n', 1)[1]))
                    props = schema['properties']
                    stage = next(stage for key, stage in [('information_needed', 'define'), ('questions', 'clarify'),
                        ('searches', 'search_queries'), ('entities', 'extract-fixture'), ('selected_facts', 'evidence-fixture'),
                        ('decisions', 'triage-fixture'), ('identity_assessments', 'report-fixture')] if key in props)
                    if 'entities' in props and 'decisions' in props:
                        stage = 'extract-omissions-fixture'
                    request = {'response_schema': schema, 'messages': [{'role': m['role'], 'content': m['content'][0]['text']} for m in wire['messages']]}
                    payload = json.loads(fixture(stage, request, sid))
                    if mode == 'tool_nullable':
                        return nova_reply(fixture_wire_value(payload, schema))
                    reply = nova_reply({}, 'end_turn')
                    reply['output']['message']['content'] = [{'text': json.dumps(payload)[1:]}]
                    return reply

                sdk = factory.return_value; sdk.converse.side_effect = send
                sessions = Sessions(root, env_file=root / 'absent', runner_factory=FixtureRun)
                try:
                    with TestClient(create_app(sessions)) as client:
                        client.post('/api/setup', json={'session_id': sid, 'question': 'Profile Acme in Canada', 'auto_advance': True,
                            'research_rounds': 1, 'model_request_budget': 100, 'model_token_budget': 1000000})
                        state = wait_for_completion(client, sid)
                        self.assertEqual(state['status'], 'report_completed', state)
                        report = client.get(f'/api/setup/{sid}/report-data').json()
                        self.assertEqual(report['presentation'], 'entity_profile')
                        self.assertEqual(report['summary'][0]['text'], 'Acme builds AI tools in Canada.')
                        ledger = read_ledger(root / sid)
                        self.assertEqual(ledger['totals']['attempted'], sdk.converse.call_count)
                        self.assertEqual(state['model_budget']['known_tokens'], sdk.converse.call_count * 40)
                        self.assertTrue(all(row['validation'] == 'accepted' for row in ledger['calls']))
                finally:
                    sessions.close()

    def test_empty_profile_preserves_sections_and_cancel_sends_no_call(self):
        """Check unknown output and cooperative cancellation through the production report worker.

        Args: None.
        Returns: None; empty evidence yields every requested section without a model
            call; pre-dispatch cancellation retains evidence and never creates a report.
        """
        from threading import Event
        from projectclarity.reporting.report_generation import ReportRun
        for cancel in (False, True):
            with TemporaryDirectory() as tmp:
                root = Path(tmp); state = many_facts(root, 0 if not cancel else 2)
                state.pop('report_scheme'); state['report_plan'] = {**PROFILE_PLAN, 'subject_kind': 'organization'}
                caller = ProfileCaller(); stop = Event()
                if cancel:
                    stop.set()
                worker = ReportRun(root / state['session_id'], state, lambda _: None, caller, stop)
                worker.execute()
                self.assertEqual(state['report']['status'], 'cancelled' if cancel else 'completed')
                self.assertEqual(caller.calls, [])
                if not cancel:
                    report = json.loads((worker.root / 'report.json').read_text())
                    self.assertEqual(len(report['profile']['sections']), len(state['information_needed']))
                    self.assertTrue(all(s['status'] == 'unknown' for s in report['profile']['sections']))
                    self.assertIn('Not established', (worker.root / 'report.csv').read_text())

    def test_missing_plan_in_new_model_reply_is_rejected_before_search(self):
        """Do not mistake a malformed new response for a historical directory session.

        Args: None.
        Returns: None; setup fails after two calls, with no search or default format.
        """
        class MissingPlanCaller(ProfileCaller):
            """Return an old-shaped clarification to test the new mandatory contract."""
            def __call__(self, stage, request, sid):
                """Remove only the required plan from a controlled clarification reply.

                Args: stage: Workflow label; request: Production request; sid: Session.
                Returns: Fixture JSON; no upstream orchestration is replaced.
                """
                result = json.loads(super().__call__(stage, request, sid))
                if stage == 'clarify':
                    result.pop('report_plan')
                return json.dumps(result)
        with TemporaryDirectory() as tmp:
            caller = MissingPlanCaller(); sessions = Sessions(Path(tmp), caller); sid = str(uuid4())
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post('/api/setup', json={'session_id': sid, 'question': 'Profile Acme', 'auto_advance': True})
                    self.assertEqual(wait_for_completion(client, sid)['status'], 'failed')
                    self.assertEqual([c[0] for c in caller.calls], ['define', 'clarify'])
            finally:
                sessions.close()

    def test_unseen_subject_kinds_use_the_same_production_profile_path(self):
        """Exercise distinct company/person/organization inputs without subject-specific routing.

        Args: None.
        Returns: None; real setup→report paths preserve requested subject, evidence
            and required unknown sections for three separate fixture populations.
            This is cross-input routing evidence, not live semantic-quality proof.
        """
        from projectclarity.documents.web_acquisition import RobotsReader
        from tests.integration.test_document_pipeline import fixture_transport
        cases = [('Cedar Dynamics', 'company', 'Ottawa robotics developer', 'Cedar Dynamics develops robots in Ottawa.'),
                 ('Mira Patel', 'person', 'Montreal optics researcher', 'Mira Patel researches optics in Montreal.'),
                 ('Tidal Institute', 'organization', 'Halifax marine research institute', 'Tidal Institute studies marine ecosystems in Halifax.')]
        for subject, kind, scope, source in cases:
            with self.subTest(subject=subject), TemporaryDirectory() as tmp:
                plan = {**PROFILE_PLAN, 'subject': subject, 'subject_kind': kind, 'subject_scope': scope}

                class SubjectCaller(ProfileCaller):
                    """Provide source-specific external responses, never a hard-coded report artifact."""
                    def __call__(self, stage, request, sid):
                        """Bind synthetic planning/extraction to the current unseen source.

                        Args: stage: Real model stage; request: Full production request;
                            sid: Owning test session.
                        Returns: Fixture model text. Downstream selection, validation,
                            batching, assembly and export execute unchanged.
                        """
                        if stage == 'define':
                            self.calls.append((stage, request, sid))
                            return json.dumps({'information_needed': [
                                {'kind': 'attribute', 'name': 'Work', 'description': 'Describe the subject activity.'},
                                {'kind': 'attribute', 'name': 'History', 'description': 'Describe the requested history, if evidenced.'}]})
                        if stage.startswith('extract-') and not stage.startswith('extract-omissions-'):
                            self.calls.append((stage, request, sid))
                            text = json.loads(request['messages'][0]['content'])['document_chunk']
                            return json.dumps({'entities': [{'id': 'subject', 'name': subject, 'type': kind, 'subtype': None}],
                                'facts': [{'subject_id': 'subject', 'predicate': 'activity', 'value': source, 'object_id': None,
                                           'statement': text, 'quote': text}]})
                        return super().__call__(stage, request, sid)

                def transport(url, limit, cancelled):
                    """Serve the held-out source through the production acquisition reader.

                    Args: url: Requested URL; limit: Byte limit; cancelled: Stop callback.
                    Returns: Fixture response bytes; robots behavior stays unchanged.
                    """
                    status, headers, body = fixture_transport(url, limit, cancelled)
                    if url.endswith('/doc'):
                        body = ('<html><body><p>' + source + '</p></body></html>').encode()
                    return status, headers, body

                class SubjectRun(FixtureRun):
                    """Keep actual acquisition/extraction with a different fixture HTTP source."""
                    def __init__(self, root, state, publish, caller, stop):
                        """Replace only the fixture transport, not workflow or report behavior.

                        Args: root: Session files; state: Saved context; publish: State writer;
                            caller: Fixture provider; stop: Actual cancellation event.
                        Returns: None; reader uses real robots/download/parsing logic.
                        """
                        super().__init__(root, state, publish, caller, stop)
                        self.documents.reader = RobotsReader(self.root, self.documents.cancelled, self.documents.event, transport)

                caller = SubjectCaller(plan=plan); sid = str(uuid4())
                sessions = Sessions(Path(tmp), caller, runner_factory=SubjectRun)
                try:
                    with TestClient(create_app(sessions)) as client:
                        client.post('/api/setup', json={'session_id': sid, 'question': f'Profile {subject}: work and history; {scope}.',
                            'auto_advance': True, 'research_rounds': 1})
                        state = wait_for_completion(client, sid)
                        self.assertEqual(state['status'], 'report_completed', state)
                        report = client.get(f'/api/setup/{sid}/report-data').json()
                        self.assertEqual(report['profile']['subject'], subject)
                        self.assertEqual(report['profile']['subject_kind'], kind)
                        self.assertEqual(report['summary'][0]['text'], source)
                        self.assertEqual(report['profile']['sections'][1]['status'], 'unknown')
                        self.assertTrue(all(not c[0].startswith('merge-') for c in caller.calls))
                finally:
                    sessions.close()


    def test_named_company_without_identity_qualifier_completes_real_workflow(self):
        """Accept the nullable scope advertised to models without weakening identity checks.

        Args: None.
        Returns: None; HTTP setup and reporting preserve null scope, CSV explains its
            absence, and blank subject/scope still fail. Uses offline replies only.
        """
        from projectclarity.reporting.contracts import validate_report_plan
        plan = {**PROFILE_PLAN, 'subject_scope': None}
        with TemporaryDirectory() as tmp:
            caller = ProfileCaller(plan=plan); sid = str(uuid4())
            sessions = Sessions(Path(tmp), caller, runner_factory=FixtureRun)
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post('/api/setup', json={'session_id': sid, 'question': 'Research Acme, its founders and products.',
                        'auto_advance': True, 'research_rounds': 1})
                    state = wait_for_completion(client, sid)
                    self.assertEqual(state['status'], 'report_completed', state)
                    self.assertIsNone(state['report_plan']['subject_scope'])
                    report = client.get(f'/api/setup/{sid}/report-data').json()
                    self.assertIsNone(report['profile']['subject_scope'])
                    self.assertIn('No further identity qualifier supplied', client.get(f'/api/setup/{sid}/report.csv').text)
            finally:
                sessions.close()
        for change in ({'subject': ''}, {'subject_kind': None}, {'subject_scope': ' '}):
            with self.assertRaises(ValueError): validate_report_plan({**plan, **change})
