"""Production CLI recovery tests with synthetic SDK responses and real validators.

Cases reproduce the observed excess-caveat and invented-witness failures without
live model calls. Mechanical caveat preservation does not prove semantic quality.
"""
import json
import unittest
from copy import deepcopy

from projectclarity.coverage.summary_recovery import preserve_caveats, scheduling_blocker
from projectclarity.coverage.bounded_contracts import BoundedDigest
from projectclarity.coverage.stopping import completion_status
from tests.research import test_contract_completion as contracts


class SummaryRecoveryTests(unittest.TestCase):
    """Require bounded correction, preserved caveats and durable failure boundaries."""

    def setUp(self):
        """Prepare the production-CLI fixture owner without any external services.

        Args:
            self: Test instance owning cleanup and provider observations.
        Returns:
            None; real orchestration is retained by the reused harness.
        """
        self.fixture = contracts.ContractCompletionTests()
        self.addCleanup(self.fixture.doCleanups)

    def test_excess_caveats_correct_once_and_reach_fresh_final(self):
        """Recover a nine-caveat response while retaining every original string.

        Args:
            self: Test owner inspecting actual saved requests and final coverage.
        Returns:
            None; proves production recovery and immutable prior traces, not LLM
            semantic accuracy. Original caveats are intentionally absent in reply2.
        """
        injected = []
        caveats = [f'Unresolved caveat {i} remains relevant.' for i in range(9)]

        def reply(payload, answer, fixture):
            """Inject one malformed output and inspect the persisted correction.

            Args:
                payload: Actual SDK request data.
                answer: Otherwise valid synthetic digest response.
                fixture: CLI harness used to read the reservation before response.
            Returns:
                Response dictionary; all primary evidence and validators stay real.
            """
            if not injected:
                injected.append(deepcopy(payload))
                answer['limitations'] = caveats
            elif 'correction' in payload:
                self.assertEqual({k:v for k,v in payload.items() if k != 'correction'}, injected[0])
                self.assertEqual(json.loads(payload['correction']['previous_response'])['limitations'], caveats)
                saved = fixture.cli.saved_session()[1]
                correcting = [r for r in saved['coverage']['evidence_batches'].values()
                    if r.get('recovery_state') == 'correcting']
                self.assertEqual(len(correcting), 1)
                self.assertEqual(correcting[0]['attempts'], 2)
                self.assertEqual(correcting[0]['attempt_history'][-1]['status'], 'reserved')
            return answer

        _, session, directory, seen = self.fixture.run_contract(count=8, large=True, digest_transform=reply)
        self.assertTrue(injected)
        corrections = [p for title,p in seen if title=='BoundedDigest' and 'correction' in p]
        self.assertEqual(len(corrections), 1)
        recovered = [r for r in session.coverage['evidence_batches'].values() if r['attempts']==2]
        self.assertEqual(len(recovered), 1)
        receipt = recovered[0]
        self.assertEqual(receipt['status'], 'completed')
        retained = '\n'.join(receipt['assessment']['limitations'])
        self.assertTrue(all(c in retained for c in caveats))
        original = json.loads((directory/'llm_calls'/f"{receipt['attempt_history'][0]['call_id']}.json").read_text())
        self.assertEqual(json.loads(original['response_text'])['limitations'], caveats)
        self.assertEqual(original['error_code'], 'invalid_output_schema')
        self.assertEqual(session.coverage['history'][-1]['status'], 'assessed')
        self.assertTrue(completion_status(session, directory.parent)['ready'])
        self.assertTrue(all(r['attempts']==1 for r in session.coverage['evidence_batches'].values()
            if r is not receipt))

    def test_invented_witness_corrects_without_relaxing_validator(self):
        """Correct an unoffered witness and preserve an earlier conflict flag.

        Args:
            self: CLI test owner.
        Returns:
            None; corrected accounting succeeds but retained conflict blocks closure.
        """
        injected = []

        def reply(payload, answer, fixture):
            """Inject one unknown witness, then supply valid original evidence IDs.

            Args:
                payload: Actual bounded request.
                answer: Valid fixture response to modify once.
                fixture: Unused CLI owner, retained for callback parity.
            Returns:
                Synthetic reply with deliberately disappearing conflict on correction.
            """
            if not injected:
                injected.append(True)
                answer.update(witness_candidate_ids=['invented'], unresolved_conflict=True)
            elif 'correction' in payload:
                answer['unresolved_conflict'] = False
            return answer

        _, session, _, seen = self.fixture.run_contract(count=8, large=True,
            fault='summary_conflict', digest_transform=reply)
        receipts = [r for r in session.coverage['evidence_batches'].values() if r['attempts']==2]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]['status'], 'completed')
        self.assertNotIn('invented', receipts[0]['assessment']['witness_candidate_ids'])
        self.assertTrue(receipts[0]['assessment']['unresolved_conflict'])
        self.assertNotEqual(session.workflow['stop_reason'], 'criteria_satisfied')
        self.assertEqual(sum('correction' in p for t,p in seen if t=='BoundedDigest'), 1)

    def test_exhausted_correction_blocks_without_repeated_checkpoints(self):
        """Keep repeated invalid output terminal across normal scheduling and resume.

        Args:
            self: Test owner running an invalid real CLI trajectory.
        Returns:
            None; only two logical requests per unchanged node occur; resume does not reset
            them. No valid peer or original receipt is overwritten.
        """
        fixture, session, directory, seen = self.fixture.run_contract(count=8, large=True, fault='digest_invalid')
        digests = [p for t,p in seen if t=='BoundedDigest']
        self.assertTrue(digests)
        self.assertEqual(scheduling_blocker(session), 'bounded_summary_recovery_exhausted')
        failed = list(session.coverage['evidence_batches'].values())
        self.assertTrue(all(r['attempts']==2 for r in failed))
        self.assertEqual(len(digests), 2 * len(failed))
        before = session.usage.requests_attempted
        fixture.cli.run_cli(fixture.provider, '--resume', session.session_id,
            plan_only=False, details=False)
        saved = json.loads((directory/'session.json').read_text())
        self.assertEqual(saved['usage']['requests_attempted'], before)
        self.assertEqual(len([p for t,p in seen if t=='BoundedDigest']), len(digests))
        changed = session.model_copy(deep=True)
        for candidate in changed.extraction['candidates']:
            candidate['proposal']['statement'] += ' Updated evidence.'
        self.assertIsNone(scheduling_blocker(changed))

    def test_exact_caveats_fail_closed_when_they_cannot_fit(self):
        """Reject loss of long caveats rather than silently clipping to eight items.

        Args:
            self: Pure validation test owner.
        Returns:
            None; demonstrates both sticky flags and an explicit non-fit failure.
        """
        answer = BoundedDigest(covered_input_ids=['C1'], summary='Summary',
            witness_candidate_ids=['C1'], limitations=[], unresolved_conflict=False,
            unresolved_identity=False, requires_detail=False)
        old = dict(limitations=['Do not discard'], unresolved_identity=True, requires_detail=True)
        kept = preserve_caveats(answer, old)
        self.assertEqual(kept.limitations, ['Do not discard'])
        self.assertTrue(kept.unresolved_identity and kept.requires_detail)
        with self.assertRaisesRegex(ValueError, 'caveats_do_not_fit'):
            preserve_caveats(answer, {'limitations':[str(i)*500 for i in range(9)]})

    def test_correction_crash_is_not_replayed_on_resume(self):
        """Crash after correction dispatch and retain its uncertain charged outcome.

        Args:
            self: Production CLI fixture owner.
        Returns:
            None; persisted reservation survives restart with no new digest calls.
        """
        def reply(payload, answer, fixture):
            """Fail the original response and interrupt the separately reserved correction.

            Args:
                payload: Actual SDK digest request.
                answer: Valid synthetic response to corrupt on the initial attempt.
                fixture: CLI owner, unused by this transport failure.
            Returns:
                Invalid first response; raises SystemExit on correction dispatch.
            """
            if 'correction' in payload:
                raise SystemExit(77)
            answer['witness_candidate_ids'] = ['invented']
            return answer

        fixture, session, directory, seen = self.fixture.run_contract(count=8, large=True,
            fault='digest_correction_crash', digest_transform=reply)
        pending = [r for r in session.coverage['evidence_batches'].values() if r['status']=='reserved']
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['attempts'], 2)
        before = len([p for t,p in seen if t=='BoundedDigest'])
        fixture.cli.run_cli(fixture.provider, '--resume', session.session_id,
            plan_only=False, details=False)
        saved = json.loads((directory/'session.json').read_text())
        self.assertEqual(len([p for t,p in seen if t=='BoundedDigest']), before)
        receipt = saved['coverage']['evidence_batches'][pending[0]['batch_id']]
        self.assertIn(receipt['status'], {'reserved', 'uncertain'})
        self.assertEqual(receipt['attempts'], 2)

    def test_correction_cannot_consume_final_coverage_reservation(self):
        """Stop a correction at the ordinary-work ceiling without budget overspend.

        Args:
            self: CLI fixture owner measuring then reproducing a deterministic boundary.
        Returns:
            None; no correction wire request occurs when only final coverage is left.
        """
        first = []

        def probe(payload, answer, fixture):
            """Record the first summary wire count using only saved production state.

            Args:
                payload: SDK request, unused in this boundary measurement.
                answer: Valid synthetic digest.
                fixture: CLI owner exposing the pre-response session snapshot.
            Returns:
                Unmodified accounting response.
            """
            if not first:
                first.append(fixture.cli.saved_session()[1]['usage']['requests_attempted'])
            return answer

        self.fixture.run_contract(count=8, large=True, digest_transform=probe)
        self.assertTrue(first)
        _, session, _, seen = self.fixture.run_contract(count=8, large=True,
            fault='digest_invalid', budget=first[0]+1)
        self.assertLessEqual(session.usage.requests_attempted, first[0]+1)
        self.assertFalse(any('correction' in p for t,p in seen if t=='BoundedDigest'))
        attempts = [a for r in session.coverage.get('evidence_batches', {}).values()
            for a in r.get('attempt_history', [])]
        self.assertTrue(any(a['error_code']=='request_limit' for a in attempts), attempts)

    def test_source_change_during_correction_cannot_accept_stale_summary(self):
        """Alter a saved source after correction dispatch and reject stale evidence.

        Args:
            self: CLI fixture owner modifying only its disposable source copy.
        Returns:
            None; a schema-valid correction cannot override source-integrity checks.
        """
        injected = []

        def reply(payload, answer, fixture):
            """Produce one invalid response, then change the actual saved source bytes.

            Args:
                payload: Serialized summary request at the SDK boundary.
                answer: Otherwise valid deterministic response.
                fixture: Temporary run owner supplying source paths.
            Returns:
                Synthetic response; correction evidence is now deliberately stale.
            """
            if not injected:
                injected.append(True)
                answer['witness_candidate_ids'] = ['invented']
            elif 'correction' in payload:
                directory, saved = fixture.cli.saved_session()
                document = saved['documents'][0]
                path = directory / document['text_path']
                path.write_text(path.read_text() + '\nChanged source version.')
            return answer

        _, session, _, _ = self.fixture.run_contract(count=8, large=True,
            fault='changed_source', digest_transform=reply)
        corrected = [r for r in session.coverage['evidence_batches'].values() if r['attempts']==2]
        self.assertEqual(len(corrected), 1)
        self.assertEqual(corrected[0]['error_code'], 'evidence_changed_during_batch')
        self.assertIsNone(corrected[0]['assessment'])

    def test_correction_envelope_is_rejected_before_credentials_if_oversized(self):
        """Count original inputs and rejected output together under the existing bound.

        Args:
            self: Test owner running the actual shared executor without networking.
        Returns:
            None; zero model attempts and no credential access on oversize recovery.
        """
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from projectclarity.providers.structured_model_call_execution import invoke_structured
        from projectclarity.discovery.inventory_context import payload_messages
        from tests.research.research_test_fixtures import example_session
        session = example_session()
        session.request = session.request.model_copy(update={
            'budget': session.request.budget.model_copy(update={'max_requests': 20})})
        payload = dict(stage='evidence', inputs=[], sources={}, context={},
            correction=dict(previous_response='x' * 128_000))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root/session.session_id).mkdir()
            answer, receipt = invoke_structured(session, root, call_id='bounded_correction',
                task='bounded_evidence', prompt_version='test',
                messages=payload_messages('Test fixture', payload), schema=BoundedDigest,
                key_reader=lambda: self.fail('Oversize input must not read credentials'))
            self.assertIsNone(answer)
            self.assertEqual(receipt['error_code'], 'input_contract_too_large')
            self.assertEqual(session.usage.requests_attempted, 0)

    def test_recovery_eligibility_requires_exact_complete_original_request(self):
        """Reject interrupted, truncated and changed-context correction sources.

        Args:
            self: Test owner creating private synthetic executor traces.
        Returns:
            None; only a complete matching rejected response supplies recovery data.
        """
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from projectclarity.providers.response_recovery import correction_data
        messages = [('system', 'Original'), ('human', '{}')]
        base = dict(call_id='bounded_test', status='failed', error_code='invalid_output_schema',
            stop_reason='end_turn', response_text='{}', response_text_truncated=False,
            messages=[dict(role=r,content=c) for r,c in messages])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'llm_calls').mkdir()
            path = root/'llm_calls/bounded_test.json'
            path.write_text(json.dumps(base))
            self.assertIsNotNone(correction_data(root, 'bounded_test', 'invalid_output_schema',
                {'invalid_output_schema'}, messages))
            self.assertIsNone(correction_data(root, 'bounded_test', 'invalid_output_schema',
                {'invalid_output_schema'}, [('system','Changed'), ('human','{}')]))
            for change in [dict(response_text_truncated=True), dict(status='started'),
                    dict(error_code='interrupted_outcome_unknown'), dict(stop_reason='max_tokens')]:
                path.write_text(json.dumps({**base, **change}))
                self.assertIsNone(correction_data(root, 'bounded_test', 'invalid_output_schema',
                    {'invalid_output_schema'}, messages))
