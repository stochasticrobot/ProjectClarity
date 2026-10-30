"""Pause research for human clarification when the proposed plan requires a decision.

The workflow calls this step after planning. If clarification is needed, it saves
a review request and uses LangGraph's interrupt mechanism to preserve the paused
execution. A later terminal or dashboard decision can answer the questions, replace
the plan or reject it. This file applies that decision; it does not display menus
or read keyboard input. A plan that needs no review can continue without a pause.
"""
from datetime import UTC, datetime
from pathlib import Path
from langgraph.types import interrupt
from projectclarity.workflow_state import ResearchState
from projectclarity.nodes.review_plan.plan_review_contracts import PlanReviewRequest, PlanReviewDecision
from projectclarity.nodes.review_plan.plan_review_service import validate_review_decision
from projectclarity.plan_renderer import render_plan
from projectclarity.session_storage import save_session, write_text


def review_plan(state: ResearchState, output_root: Path, monitor=None) -> dict:
    """Pause for a UI decision when necessary, without embedding any UI code.

    LangGraph restarts this function when the decision arrives. Therefore all durable side
    effects occur AFTER interrupt(), which returns that decision on resume. The outer runner
    announces the pause after checkpointing succeeds. A plan without questions passes
    through; that is not recorded as human approval. Bind saved answers to the
    original review questions before a revised plan can replace those questions.

    Args:
        state: Graph-state dictionary whose session entry is a typed ResearchSession for the
            current run.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        monitor: Optional RunMonitor for progress events and cooperative cancellation; None
            disables those hooks.

    Returns:
        A dictionary with one session entry containing the updated ResearchSession.
        LangGraph merges this update into its state; it is not an answer report or published
        knowledge.
    """
    session = state["session"]
    if session.pending_review:
        review = PlanReviewRequest.model_validate(session.pending_review)
        decision = PlanReviewDecision.model_validate(interrupt(review.model_dump(mode="json")))
        validate_review_decision(review, decision, session)
        # Retain original question meaning even when a revised plan replaces it.
        questions = {question.question_id: question.question for question in review.questions}
        for answer in decision.answers:
            answer.question = questions[answer.question_id]
        session.review_decision = decision.model_dump(mode="json")
        session.review_resolved_at = datetime.now(UTC).isoformat()
        session.pending_review = None
        if decision.revised_plan is not None:
            session.plan = decision.revised_plan
        session.status = "rejected" if decision.action == "reject" else "plan_ready"
        directory = output_root / session.session_id
        write_text(directory / "review_decision.json", json_text(session.review_decision))
        write_text(directory / "plan.json", session.plan.model_dump_json(indent=2) + "\n")
        if monitor:
            monitor.observe("review_resolved", review_id=decision.review_id, review_action=decision.action,
                            run_status=session.status, stage="review_plan")
    if "review_plan" not in session.visited_nodes:
        session.visited_nodes.append("review_plan")
    save_session(session, output_root)
    write_text(output_root / session.session_id / "plan.md", render_plan(session))
    return {"session": session}


def json_text(value: dict) -> str:
    """Serialize a private JSON artifact; telemetry receives only its identifiers.

    Args:
        value: JSON-serializable application data to include in the private artifact.

    Returns:
        Indented JSON text ending in a newline, ready to save as a private artifact.
    """
    import json
    return json.dumps(value, ensure_ascii=True, indent=2) + "\n"

