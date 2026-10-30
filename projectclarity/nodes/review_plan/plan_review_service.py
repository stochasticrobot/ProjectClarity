"""Read and submit human decisions for research paused at plan review.

A plan may need clarification before source reading can continue. Terminal and
dashboard interfaces use get_plan_review to obtain the saved questions and
submit_plan_review to apply answers, a replacement plan or rejection. The service
validates the run/review IDs against LangGraph's actual saved interruption, so an
old form or edited display file cannot approve a different plan.

Submission holds the worker lock and runs the remaining workflow synchronously.
A browser-facing caller must arrange worker execution rather than hold an HTTP
request open for the entire research run. These functions do not display prompts
or read keyboard input. This is local checkpoint-based plan review, not a general
replay mechanism for arbitrary interrupted tasks or a distributed lock service.
"""

import hashlib
import json
from uuid import uuid4

from langgraph.types import Command
from langsmith import tracing_context

from projectclarity.nodes.review_plan.plan_review_contracts import PlanReviewDecision, PlanReviewRequest, ReviewQuestion
from projectclarity.research_schemas import ResearchSession
from projectclarity.nodes.plan_research.research_planner import ResearchError
from projectclarity.run_monitor import RunMonitor, utc_now
from projectclarity.run_status_commands import run_directory
from projectclarity.workflow_checkpoints import checkpoint_store, thread_config


def create_plan_review(session: ResearchSession) -> dict:
    """Create a stable private request once, before the review node can interrupt.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.

    Returns:
        A JSON-compatible review-request dictionary containing the run/review IDs, plan
        fingerprint and numbered questions.
    """
    canonical = json.dumps(session.plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return PlanReviewRequest(run_id=session.session_id, review_id=uuid4().hex,
        plan_sha256=hashlib.sha256(canonical.encode()).hexdigest(), created_at=utc_now(),
        plan=session.plan, questions=[ReviewQuestion(question_id=f"Q{i}", question=question)
            for i, question in enumerate(session.plan.clarifications, 1)]).model_dump(mode="json")


def validate_review_decision(review: PlanReviewRequest, decision: PlanReviewDecision,
                             session: ResearchSession) -> None:
    """Enforce the pending review's identity and scope before resuming any node.

    Args:
        review: Pending PlanReviewRequest identifying the exact plan and allowed
            clarification scope.
        decision: Parsed PlanReviewDecision submitted by the user or UI for that review.
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.

    Returns:
        None when the decision belongs to this pending review and respects its constraints;
        otherwise raise a validation error. Designed-run revisions must retain the
        accepted report, completion checks and valid goal-to-requirement mappings.
    """
    if decision.run_id != review.run_id or decision.review_id != review.review_id:
        raise ResearchError("This decision does not match the pending review. Read the current review again.")
    if decision.action == "answer":
        if {answer.question_id for answer in decision.answers} != {q.question_id for q in review.questions}:
            raise ResearchError("Answer every listed question exactly once, using its Q identifier.")
    if decision.revised_plan is not None:
        if session.answer_schema:
            proposed = decision.revised_plan.model_copy(deep=True)
            from projectclarity.answer_schema.binding import bind_report
            from projectclarity.nodes.schedule_work.answer_controller import enabled
            try:
                bind_report(proposed, session.answer_schema,
                    require_mappings=enabled(session))
            except ValueError:
                raise ResearchError('A revised plan must retain the designed report sections and valid requirement mappings.') from None
            if proposed != decision.revised_plan:
                raise ResearchError('This revision changes the designed answer contract. Start a new run with the revised question.')
        known = {source.source_id for source in session.sources}
        if any(source_id not in known for step in decision.revised_plan.strategy for source_id in step.source_ids):
            raise ResearchError("A revised plan may reference only the originally supplied source IDs.")


def pending_review_state(graph, run_id: str, monitor: RunMonitor):
    """Require both a durable native interrupt and a deliberately paused worker.

    Args:
        graph: Compiled graph connected to the existing checkpoint store.
        run_id: Saved run identifier naming one subdirectory under output_root.
        monitor: Optional RunMonitor for progress events and cooperative cancellation; None
            disables those hooks.

    Returns:
        A pair of the paused ResearchSession and its PlanReviewRequest, both checked against
        the durable graph interrupt.
    """
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
    """Read the authoritative private payload without changing events or invoking an LLM.

    Args:
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        run_id: Saved run identifier naming one subdirectory under output_root.

    Returns:
        The current PlanReviewRequest loaded from the durable pause; no model is called.
    """
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

    Completed, busy, older, or unreadable runs are not resumable menu entries. This is a
    local convenience index, not a health report or distributed queue. A final submit still
    rechecks identity and takes the lock, since the menu can become out of date while the
    person is deciding.

    Args:
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.

    Returns:
        Pending PlanReviewRequest objects in newest-first order; an absent output directory
        yields an empty list.
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
    """Validate a UI response and resume the same graph/thread, without rerunning planning.

    Args:
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        decision: Validated PlanReviewDecision identifying the pending review and explicit
            user action.
        on_event: Optional callback receiving operational progress-event dictionaries, not
            source text or private prompts.

    Returns:
        The ResearchSession after applying the decision and resuming or cancelling the same
        saved run. The native review interrupt remains durable across processes.
        Continuation saves its next graph checkpoint on exit or another pause,
        rather than copying the research session after every processing step.
    """
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
                result = graph.invoke(Command(resume=decision.model_dump(mode="json")),
                    thread_config(decision.run_id), durability="exit")
                return finish_graph_invocation(result, output_root, monitor, graph)
    except BlockingIOError:
        raise ResearchError("This run already has an active worker. No review was submitted.") from None
    except BaseException as exc:
        if started:
            monitor.finish("cancelled" if isinstance(exc, (KeyboardInterrupt, EOFError)) else "failed", error_type=type(exc).__name__)
        raise
    finally:
        monitor.close()
