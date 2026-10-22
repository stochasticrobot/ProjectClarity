"""Check what a person sees when reviewing a plan in a terminal or Markdown.

For example, the question's category should be visible beside the objective,
and a failed source should not look like successful research. These tests call
the renderer directly with small sessions; the shared fixture reads a handwritten
plan JSON, but rendering does no disk/network I/O and runs no graph or model.
Assertions focus on useful content and boundaries, not exact whitespace
throughout the whole report, so ordinary layout changes remain inexpensive.
"""

import unittest

from projectclarity.plan_renderer import render_plan, terminal_text
from projectclarity.research_schemas import RetrievedDocument, URLSource
from tests.research.research_test_fixtures import example_session


class PlanRenderingTests(unittest.TestCase):
    """Rendering describes current results; it never validates factual claims."""

    def test_category_and_question_are_visible_as_distinct_plan_sections(self):
        """Readers can distinguish the question's type from its topic and goals."""
        session = example_session()
        session.plan.question_category = "Comparison"
        text = render_plan(session)
        self.assertIn("## Question category\nComparison", text)
        self.assertIn(session.request.question, text)
        self.assertIn(session.plan.objective, text)
        self.assertIn("Planning only", text)
        self.assertNotIn("## Retrieval results", text)

    def test_missing_plan_is_not_rendered_as_a_successful_empty_plan(self):
        """A caller must handle planning failure instead of printing a fake plan."""
        with self.assertRaisesRegex(ValueError, "without a validated plan"):
            render_plan(example_session(with_plan=False))

    def test_terminal_filter_removes_controls_but_keeps_readable_unicode(self):
        """Untrusted text cannot send escape/control bytes to the user's terminal."""
        text = "Café\tCanada\n\x1b[31mred\x1b[0m\x00\x07\r\x7f\x9b"
        filtered = terminal_text(text)
        self.assertEqual(filtered, "Café\tCanada\n[31mred[0m")

    def test_partial_retrieval_displays_successes_failures_and_scope_limits(self):
        """A readable source and a blocked source remain distinct, unverified outcomes."""
        session = example_session(plan_only=False)
        session.visited_nodes.append("retrieve_sources")
        session.status = "retrieval_partial"
        session.sources.append(URLSource(source_id="S2", url="https://example.org/blocked"))
        session.documents = [
            RetrievedDocument(source_id="S1", source_kind="url", original_location=session.sources[0].url,
                attempted_at=session.created_at, status="retrieved", character_count=12,
                text_path="documents/S1/text.txt"),
            RetrievedDocument(source_id="S2", source_kind="url", original_location=session.sources[1].url,
                attempted_at=session.created_at, error_code="robots_denied", error="Source disallows access."),
        ]
        text = render_plan(session)
        self.assertIn("1/2 sources produced readable text", text)
        self.assertIn("documents/S1/text.txt", text)
        self.assertIn("S2: failed [robots_denied]", text)
        self.assertIn("unverified material", text)
        self.assertIn("no factual claims have been extracted or verified", text)
