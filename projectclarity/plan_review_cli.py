"""Adapt the shared plan-review API to terminal commands.

`review RUN_ID` opens guided review in a terminal. Without a terminal (or with
--non-interactive), it only displays the pending decision. --json returns the
full typed payload for a script. Explicit action flags still submit a decision
without prompts. terminal_research_interface.py owns all question/menu handling.

This is presentation code, not a graph node. A web UI can replace it without
changing review_plan or its request/decision contracts. Full review text is
private research data; unlike progress events it may contain sensitive text.
"""

import json
from pathlib import Path

from projectclarity.plan_renderer import terminal_text
from projectclarity.plan_review_contracts import PlanReviewDecision, ReviewAnswer
from projectclarity.plan_review_service import get_plan_review, submit_plan_review
from projectclarity.research_planner import ResearchError
from projectclarity.terminal_research_interface import interactive_plan_review, use_interactive_review


def add_review_command(commands) -> None:
    """Keep review-specific flags separate from source-acquisition CLI options."""
    parser = commands.add_parser("review", help="Inspect or resolve a paused plan without replanning; continue the configured workflow")
    parser.add_argument("run_id")
    parser.add_argument("--output-dir", type=Path, default=Path(".projectclarity/runs"))
    parser.add_argument("--json", action="store_true", help="Print the private pending review payload; inspection only")
    parser.add_argument("--non-interactive", action="store_true", help="Inspect without a guided menu, even in a terminal")
    parser.add_argument("--review-id", help="Required with a decision; binds it to the review you inspected")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--accept-assumptions", action="store_true", help="Explicitly accept listed assumptions and waive unanswered questions")
    actions.add_argument("--answers", type=Path, help='UTF-8 JSON object mapping every question ID to an answer, e.g. {"Q1":"2024–2026"}')
    actions.add_argument("--reject", action="store_true", help="End this run without retrieving any documents")
    actions.add_argument("--decision", type=Path, help="Full versioned PlanReviewDecision JSON (supports a manually revised plan)")
    parser.add_argument("--note", help="Optional private explanation accompanying the decision")
    progress = parser.add_mutually_exclusive_group()
    progress.add_argument("--quiet-progress", action="store_true")
    progress.add_argument("--progress-json", action="store_true")
    progress.add_argument("--show-prompts", action="store_true", help="Show private model inputs/replies for new calls after resuming")
    parser.add_argument("--details", action="store_true", help="Print the full graph, plan and extraction results after continuation")


def read_decision_file(path: Path):
    """Bound UI input before decoding; never execute a response file as code."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ResearchError("Review response files must be at most 64 KiB.")
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ResearchError("Cannot read review response. Use a readable UTF-8 JSON file.") from None


def run_review_command(args, *, on_event=None):
    """Inspect, offer guided review, or submit an explicit decision using the same service."""
    action = "accept_assumptions" if args.accept_assumptions else "answer" if args.answers else "reject" if args.reject else None
    if action is None and args.decision is None:
        if args.review_id or args.note:
            raise ResearchError("Choose a review action when supplying --review-id or --note.")
        if use_interactive_review(args):
            return interactive_plan_review(args.output_dir, args.run_id, on_event=on_event)
        review = get_plan_review(args.output_dir, args.run_id)
        if args.json:
            print(review.model_dump_json(indent=2))
        else:
            lines = [f"Run: {review.run_id}", f"Review ID: {review.review_id}",
                     f"Objective: {review.plan.objective}", "", "Questions:"]
            lines.extend(f"  {item.question_id}: {item.question}" for item in review.questions)
            lines.extend(["", "Assumptions:", *[f"  - {value}" for value in review.plan.assumptions], "",
                          "Decide with --review-id ID and --answers FILE, --accept-assumptions, or --reject.",
                          "Answers are recorded human guidance, not automatic LLM replanning.",
                          "Only original supplied sources can be fetched; budgets and access checks remain enforced."])
            print(terminal_text("\n".join(lines)))
        return None
    if args.json:
        raise ResearchError("--json is for inspecting a review, not submitting a decision.")
    if args.decision:
        if args.review_id or args.note:
            raise ResearchError("Put review_id and note inside --decision JSON, not separate flags.")
        decision = PlanReviewDecision.model_validate(read_decision_file(args.decision))
        if decision.run_id != args.run_id:
            raise ResearchError("The decision file belongs to a different run.")
    else:
        if not args.review_id:
            raise ResearchError("Include --review-id from the review you inspected before deciding.")
        answers = []
        if args.answers:
            values = read_decision_file(args.answers)
            if not isinstance(values, dict):
                raise ResearchError("Answers must be a JSON object mapping question IDs to text.")
            answers = [ReviewAnswer(question_id=key, answer=value) for key, value in values.items()]
        decision = PlanReviewDecision(run_id=args.run_id, review_id=args.review_id,
                                      action=action, answers=answers, note=args.note)
    return submit_plan_review(args.output_dir, decision, on_event=on_event)
