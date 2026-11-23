"""Exercise support labels through production persistence without live model calls."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from projectclarity.claim_review.claim_triage import TriageRun, Decision
from projectclarity.claim_review.claim_investigation import InvestigationRun, InvestigationDecision
from projectclarity.reporting.entity_profile.contracts import IdentityAssessment
from projectclarity.reporting.entity_profile.selection import validate_profile_batch
from projectclarity.reporting.entity_profile.assembly import assemble_profile
from tests.integration.test_claim_triage import selected_fixture, TriageCaller


class CategorizedCaller(TriageCaller):
    """Supply categories while leaving production decisions and persistence intact."""

    def __call__(self, stage, request, sid):
        """Annotate fixture replies with unresolved location and name categories.

        Args: stage: Production stage; request: Unmodified input; sid: Session ID.
        Returns: JSON with labels; no network or independent validation occurs.
        """
        raw = json.loads(super().__call__(stage, request, sid))
        if stage.startswith('triage-'):
            for row in raw['decisions']:
                row['failure_categories'] = ['ambiguous_location', 'ambiguous_name']
        elif stage.startswith('investigate-'):
            raw['failure_categories'] = ['ambiguous_date']
        return json.dumps(raw)


class SupportCategoryTests(unittest.TestCase):
    """Check new labels, backward compatibility and unchanged rejection routing."""

    def test_real_triage_and_investigation_preserve_labels(self):
        """Persist labels and pass initial diagnostics into the real reviewer.

        Args: None.
        Returns: None; stage artifacts retain separate initial and remaining
            problems, while all fixture claims remain rejected as before.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, _ = selected_fixture(root, count=1)
            caller = CategorizedCaller(['needs_verification'])
            session = root / state['session_id']
            TriageRun(session, state, lambda _: None, caller, Event()).execute()
            data = json.loads((session / state['triage']['artifact_directory'] / 'decisions.json').read_text())
            self.assertTrue(data['facts'])
            for row in data['facts']:
                self.assertEqual(row['triage_failure_categories'], ['ambiguous_location', 'ambiguous_name'])
            InvestigationRun(session, state, lambda _: None, caller, Event()).execute()
            folder = session / state['investigation']['artifact_directory']
            assessments = []
            for path in folder.glob('*.json'):
                content = json.loads(path.read_text())
                if isinstance(content, dict) and 'assessment' in content:
                    assessments.append(content['assessment'])
            # Saved per-claim receipts carry the entire structured assessment.
            if not assessments:
                for path in folder.rglob('*.json'):
                    content = json.loads(path.read_text())
                    if isinstance(content, dict) and isinstance(content.get('claims'), list):
                        assessments.extend(c['assessment'] for c in content['claims'] if c.get('assessment'))
            self.assertTrue(assessments)
            for row in assessments:
                self.assertEqual(row['decision'], 'reject')
                self.assertEqual(row['failure_categories'], ['ambiguous_date'])
            requests = [json.loads(req['messages'][0]['content']) for stage, req, _ in caller.calls
                        if stage.startswith('investigate-')]
            self.assertTrue(requests)
            self.assertEqual(requests[0]['claim']['triage_failure_categories'],
                             ['ambiguous_location', 'ambiguous_name'])

    def test_profile_uncertainty_retains_categories_and_legacy_defaults(self):
        """Carry alias uncertainty to the assembled profile without inferring identity.

        Args: None.
        Returns: None; categories survive assembly; old replies remain readable
            with empty, unclassified labels and unknown enum values are rejected.
        """
        row = {'fact_id': 'F1', 'status': 'ambiguous', 'reason': 'Legal suffix needs context.',
               'identity_quote': None, 'failure_categories': ['ambiguous_name']}
        packet = {'report_plan': {'subject': 'Solink', 'subject_kind': 'company', 'subject_scope': None},
                  'requirements': [{'id': 'R1', 'name': 'Company'}]}
        facts = [{'id': 'F1', 'evidence': [{'quote': 'Solink Corp.'}]}]
        raw = {'identity_assessments': [row], 'overview': [], 'sections': [], 'unused_fact_ids': []}
        result = assemble_profile([validate_profile_batch(json.dumps(raw), packet, facts)], packet)
        self.assertEqual(result['profile']['identity_uncertainty'][0]['failure_categories'], ['ambiguous_name'])
        row.pop('failure_categories')
        self.assertEqual(IdentityAssessment.model_validate(row).failure_categories, [])
        self.assertEqual(Decision(fact_id='F1', bucket='report_knowledge', reason='Supported.').failure_categories, [])
        self.assertEqual(InvestigationDecision(fact_id='F1', decision='reject', reason='Missing.', citations=[]).failure_categories, [])
        with self.assertRaises(ValueError):
            IdentityAssessment.model_validate({**row, 'failure_categories': ['ambigious_name']})
