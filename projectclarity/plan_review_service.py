"""Connect any user interface to a pending LangGraph plan review.

The terminal calls get_plan_review() to display questions and submit_plan_review()
to send a decision. A future authenticated HTTP handler can call those same
functions with the same JSON contracts. Nothing here calls input(), prints a
screen, or depends on a browser. Submitting runs the remaining graph synchronously;
a web server will need a worker adapter, not a long-running HTTP request.

The checkpoint is authoritative. Editable display files are not approval tokens.
Responses must match the pending run/review IDs, and one process holds the worker
lock throughout submission. Old, repeated, or concurrent decisions cannot start
another fetch. A crash during execution is not automatically replayed: safe
arbitrary-task recovery and distributed locks are later work.
"""

import hashlib
import json
from uuid import uuid4

from langgraph.types import Command
from langsmith import tracing_context

from projectclarity.plan_review_contracts import PlanReviewDecision, PlanReviewRequest, ReviewQuestion
from projectclarity.research_schemas import ResearchSession
from projectclarity.research_planner import ResearchError
from projectclarity.run_monitor import RunMonitor, utc_now
from projectclarity.run_status_commands import run_directory
from projectclarity.workflow_checkpoints import checkpoint_store, thread_config


def create_plan_review(session: ResearchSession) -> dict:
    """Create a stable private request once, before the review node can interrupt."""
    canonical = json.dumps(session.plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return PlanReviewRequest(run_id=session.session_id, review_id=uuid4().hex,
        plan_sha256=hashlib.sha256(canonical.encode()).hexdigest(), created_at=utc_now(),
        plan=session.plan, questions=[ReviewQuestion(question_id=f"Q{i}", question=question)
            for i, question in enumerate(session.plan.clarifications, 1)]).model_dump(mode="json")


def validate_review_decision(review: PlanReviewRequest, decision: PlanReviewDecision,
                             session: ResearchSession) -> None:
    """Enforce the pending review's identity and scope before resuming any node."""
    if decision.run_id != review.run_id or decision.review_id != review.review_id:
        raise ResearchError("This decision does not match the pending review. Read the current review again.")
    if decision.action == "answer":
        if {answer.question_id for answer in decision.answers} != {q.question_id for q in review.questions}:
            raise ResearchError("Answer every listed question exactly once, using its Q identifier.")
    if decision.revised_plan is not None:
        known = {source.source_id for source in session.sources}
        if any(source_id not in known for step in decision.revised_plan.strategy for source_id in step.source_ids):
            raise ResearchError("A revised plan may reference only the originally supplied source IDs.")


def pending_review_state(graph, run_id: str, monitor: RunMonitor):
    """Require both a durable native interrupt and a deliberately paused worker."""
    snapshot = graph.get_state(thread_config(run_id))
    interrupts = [item for task in snapshot.tasks for item in task.interrupts]
    if not monitor.snapshot.get("waiting_for_input") or monitor.snapshot["finished"] or len(interrupts) != 1:
        raise ResearchError("No pending resumable review. This run is completed, interrupted, or predates review support.")
    if snapshot.next != ("review_plan",):
        raise ResearchError("The saved workflow is not paused at plan review.")
    session = ResearchSession.model_validate(snapshot.values["session"])
    review = PlanReviewRequest.model_validate(interrupts[0].value)
    if session.pending_review != review.model_dump(mode="json") or review.run_id != run_id:
        raise ResearchError("Review data does not match the saved checkpoint.")
    return session, review


def get_plan_review(output_root, run_id: str) -> PlanReviewRequest:
    """Read the authoritative private payload without changing events or invoking an LLM."""
    from projectclarity.agentic_workflow import build_graph

    directory = run_directory(output_root, run_id)
    monitor = RunMonitor(output_root)
    try:
        monitor.attach(directory)
        with checkpoint_store(output_root) as store:
            graph = build_graph(output_root, checkpointer=store)
            _, review = pending_review_state(graph, run_id, monitor)
            return review
    except BlockingIOError:
        raise ResearchError("This run is busy. Wait for its worker before reviewing it.") from None
    finally:
        monitor.close()


def list_pending_plan_reviews(output_root) -> list[PlanReviewRequest]:
    """Find selectable local reviews, validating each through the authoritative service.

    Completed, busy, older, or unreadable runs are not resumable menu entries.
    This is a local convenience index, not a health report or distributed queue.
    A final submit still rechecks identity and takes the lock, since the menu can
    become out of date while the person is deciding.
    """
    if not output_root.exists():
        return []
    reviews = []
    for path in output_root.iterdir():
        if not path.is_dir() or path.is_symlink():
            continue
        try:
            reviews.append(get_plan_review(output_root, path.name))
        except (ResearchError, OSError, ValueError, KeyError):
            continue
    return sorted(reviews, key=lambda review: (review.created_at, review.run_id), reverse=True)


def submit_plan_review(output_root, decision: PlanReviewDecision, *, on_event=None) -> ResearchSession:
    """Validate a UI response and resume the same graph/thread, without rerunning planning."""
    from projectclarity.agentic_workflow import build_graph, finish_graph_invocation

    # Revalidate even typed input: future transports must not bypass our contract
    # via an unchecked dictionary or a model constructed without validation.
    decision = PlanReviewDecision.model_validate(decision.model_dump(mode="json"))
    directory = run_directory(output_root, decision.run_id)
    monitor = RunMonitor(output_root, sink=on_event)
    started = False
    try:
        monitor.attach(directory)
        with checkpoint_store(output_root) as store:
            graph = build_graph(output_root, monitor=monitor, checkpointer=store)
            session, review = pending_review_state(graph, decision.run_id, monitor)
            validate_review_decision(review, decision, session)
            with monitor.termination_handler(), tracing_context(enabled=False):
                started = True
                monitor.resume()
                result = graph.invoke(Command(resume=decision.model_dump(mode="json")), thread_config(decision.run_id))
                return finish_graph_invocation(result, output_root, monitor, graph)
    except BlockingIOError:
        raise ResearchError("This run already has an active worker. No review was submitted.") from None
    except BaseException as exc:
        if started:
            monitor.finish("cancelled" if isinstance(exc, (KeyboardInterrupt, EOFError)) else "failed", error_type=type(exc).__name__)
        raise
    finally:
        monitor.close()
