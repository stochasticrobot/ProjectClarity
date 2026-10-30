"""Check when evidence-based early stopping is allowed in the research workflow.

Offline service replies drive the real graph, source checks and scheduler. Tests
compare enabled and disabled stopping and invalidate source text, reviews or
requirements after assessment. A model's satisfied label must not bypass those
checks. The tests do not prove live semantic sufficiency.
"""
import unittest

from projectclarity.coverage.stopping import completion_ready, evidence_revision
from projectclarity.research_schemas import ResearchSession
from tests.discovery import test_search_coverage_pipeline as search_flow


class CoverageStoppingTests(unittest.TestCase):
    """No stop from estimates, stale evidence, missing review or truncated inventories."""

    def run_case(self, *flags):
        """Run actual search, retrieval, review and coverage with transport fixtures.

        Args:
            self: Case owning temporary runs and synthetic services.
            *flags: Explicit production terminal flags controlling semantic stopping.
        Returns:
            Fixture, run directory and typed session after successful graph execution.
        """
        fixture = search_flow.SearchCoveragePipelineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.review = True
        code, _, err, directory, saved = fixture.run_pipeline(*flags, budget=20)
        self.assertEqual(code, 0, err)
        return fixture, directory, ResearchSession.model_validate(saved)

    def test_enabled_stops_on_supported_current_criteria(self):
        """The production scheduler consumes validated coverage only when enabled.

        Args:
            self: Case enabling semantic stopping on a source-backed roster.
        Returns:
            None. Assert explicit stop reason and a still-valid gate decision.
        """
        fixture, _, session = self.run_case('--stop-when-satisfied')
        self.assertEqual(session.workflow['stop_reason'], 'criteria_satisfied')
        self.assertTrue(completion_ready(session, fixture.cli.output))

    def test_disabled_continues_under_ordinary_resource_limits(self):
        """An equally satisfied assessment does not implicitly enable automatic stopping.

        Args:
            self: Case explicitly disabling semantic stopping.
        Returns:
            None. Assert no criterion-driven stop and unchanged valid assessment.
        """
        fixture, _, session = self.run_case('--no-stop-when-satisfied')
        self.assertNotEqual(session.workflow['stop_reason'], 'criteria_satisfied')
        self.assertFalse(completion_ready(session, fixture.cli.output))
        self.assertEqual(session.coverage['criteria_progress'][0]['status'], 'model_assessed_satisfied')

    def test_changed_or_unreviewed_evidence_prevents_stopping(self):
        """Freshness, review and actual source integrity are independent stop gates.

        Args:
            self: Case perturbing the saved result after a valid production assessment.
        Returns:
            None. Assert each invalid state prevents a completion decision.
        """
        fixture, directory, session = self.run_case('--stop-when-satisfied')
        changed = session.model_copy(deep=True)
        changed.extraction['candidates'][0]['adjudication_status'] = 'pending'
        self.assertFalse(completion_ready(changed, fixture.cli.output))
        changed.coverage['history'][-1]['evidence_revision'] = evidence_revision(changed)
        self.assertFalse(completion_ready(changed, fixture.cli.output))
        changed = session.model_copy(deep=True)
        changed.coverage['history'][-1]['inventory_truncated'] = True
        self.assertFalse(completion_ready(changed, fixture.cli.output))
        (directory / 'documents/S1/text.txt').write_text('Evidence changed after assessment.')
        self.assertFalse(completion_ready(session, fixture.cli.output))

    def test_estimates_and_new_scope_requirements_cannot_reuse_an_old_pass(self):
        """Numerical priors never substitute for assessments or freeze old scope rules.

        Args:
            self: Real completed roster fixture with intact source evidence.
        Returns:
            None. Assert an estimate-only state cannot stop and a changed study
            requirement invalidates the previously successful evidence fingerprint.
        """
        fixture, _, session = self.run_case('--stop-when-satisfied')
        changed = session.model_copy(deep=True)
        changed.coverage.update(history=[], criteria_progress=[], assessment_status='not_assessed',
            scope_estimates=[dict(lower=1, upper=1, basis='provisional_prior')])
        self.assertFalse(completion_ready(changed, fixture.cli.output))
        previous = evidence_revision(session)
        session.coverage_study['readiness'] = dict(coverage_checks=[],
            unresolved_questions=['The roster date still needs confirmation.'])
        self.assertNotEqual(previous, evidence_revision(session))
        self.assertFalse(completion_ready(session, fixture.cli.output))
