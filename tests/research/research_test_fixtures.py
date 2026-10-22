"""Build small, artificial research sessions for focused component tests.

For example, storage tests need a session to save and renderer tests need a plan
to display. Both use the same handwritten plan as the mocked CLI checks, without
calling a model or reading the developer's saved runs. Each call returns fresh
objects so one test's changes cannot affect another test.
"""

import json
from pathlib import Path

from projectclarity.research_schemas import ResearchPlan, ResearchRequest, ResearchSession, URLSource


def example_session(*, with_plan=True, plan_only=True):
    """Return a reviewable fixture; its labels are test data, not model findings."""
    fixture = Path(__file__).parents[1] / "planning/fixtures/valid_research_plan.json"
    plan_data = json.loads(fixture.read_text(encoding="utf-8"))
    plan_data["clarifications"] = []
    return ResearchSession(
        session_id="offline-run",
        created_at="2026-10-21T12:00:00+00:00",
        request=ResearchRequest(question="Who researches AI security in Canada?", plan_only=plan_only),
        sources=[URLSource(source_id="S1", url="https://example.org/research")],
        mode="plan_only" if plan_only else "plan_and_retrieve",
        plan=ResearchPlan.model_validate(plan_data) if with_plan else None,
        status="plan_ready" if with_plan else "initialized",
        visited_nodes=["initialize_research", "plan_research"] if with_plan else ["initialize_research"],
    )
