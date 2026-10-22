"""Guide a person through research setup and plan review in an ordinary terminal.

Run `python -m projectclarity` to choose new research or a saved review. When the
planner asks questions, this interface asks for answers one at a time, shows a
summary, and lets you edit or submit them. It carries run/review IDs internally;
you do not need JSON files or copied identifiers to continue.
Supplying `--question` and sources at launch skips setup in command_line.py;
this same review interface is still used if that research plan needs a decision.

This presentation layer owns research-menu and review input; the credential
reader separately handles hidden key entry when needed. It constructs the same typed
decisions used by plan_review_service.py; the graph, checkpoint, budgets and
source permissions are unchanged. Nothing is approved by pressing Enter on an
empty menu. Leaving review keeps the native pause; unfinished answers are kept
only in memory and are not submitted. A future web UI replaces this file, not
the workflow or its contracts. No model calls happen inside the review prompts.
"""

import sys
from pathlib import Path

from pydantic import ValidationError

from projectclarity.plan_renderer import terminal_text
from projectclarity.plan_review_contracts import PlanReviewDecision, ReviewAnswer
from projectclarity.plan_review_service import get_plan_review, submit_plan_review, list_pending_plan_reviews
from projectclarity.research_schemas import ResearchRequest
from projectclarity.research_planner import ResearchError
from projectclarity.source_reference_loader import load_sources


class ReviewDeferred(Exception):
    """Leave the CLI while keeping the review pending; this is not run cancellation."""

    def __init__(self, *, interrupted=False):
        """Tell the CLI to leave the durable review paused, with a distinct exit code."""
        super().__init__("Review left paused. No decision submitted; unfinished answers were not saved. "
                         "Open the ProjectClarity menu to continue later.")
        self.exit_code = 130 if interrupted else 2


def terminal_available() -> bool:
    """Never consume a pipe or print prompts into redirected/script output."""
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def use_interactive_review(args) -> bool:
    """Structured streams and an explicit script switch override terminal detection."""
    return (terminal_available() and not getattr(args, "non_interactive", False)
            and not getattr(args, "progress_json", False) and not getattr(args, "json", False))


def say(text="") -> None:
    """Print private UI text without allowing model/source strings to control the terminal."""
    print(terminal_text(text), flush=True)


def choose(label: str, options: list[str]) -> int:
    """Return a zero-based explicit choice; blank and invalid input simply retry."""
    say(label)
    for number, option in enumerate(options, 1):
        say(f"  {number}. {option}")
    while True:
        value = input(f"Choose 1-{len(options)}: ").strip()
        if len(value) <= 6 and value.isascii() and value.isdigit() and 1 <= int(value) <= len(options):
            return int(value) - 1
        say(f"Please enter a number from 1 to {len(options)}.")


def confirm(label: str) -> bool:
    """Require an affirmative response; Enter never authorizes a consequential choice."""
    while True:
        answer = input(label + " [y/N]: ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"", "n", "no"}:
            return False
        say("Please answer y or n.")


def show_review(review, *, expanded=False) -> None:
    """Show the proposal and questions in plain language, not as a JSON object."""
    say("\nPlan review")
    say(f"Objective: {review.plan.objective}")
    say(f"Question category: {review.plan.question_category}")
    for label, items in (("In scope", review.plan.scope_in), ("Out of scope", review.plan.scope_out),
                         ("Assumptions", review.plan.assumptions)):
        say(f"\n{label}:")
        for item in items:
            say(f"  - {item}")
        if not items:
            say("  None listed.")
    say("\nQuestions:")
    for number, question in enumerate(review.questions, 1):
        say(f"  {number}. {question.question}")
    if expanded:
        say("\nResearch goals:")
        for goal in review.plan.goals:
            say(f"  - {goal.question}")
            for evidence in goal.evidence_required:
                say(f"    Evidence needed: {evidence}")
        say("\nProposed strategy:")
        for number, step in enumerate(review.plan.strategy, 1):
            say(f"  {number}. {step.action}")
            if step.proposed_searches:
                say("    Suggested searches (not executed): " + "; ".join(step.proposed_searches))
    say("\nContinuing fetches only the supplied sources. Answers are saved as human guidance; "
        "they do not automatically rewrite the plan.")


def ask_answer(question, number: int, total: int, current=None):
    """Validate each answer locally, with a way back that does not approve anything."""
    say(f"\nQuestion {number} of {total}: {question.question}")
    if current is not None:
        say(f"Current answer: {current.answer}")
    while True:
        value = input("Your answer (/back for menu" + ", Enter to keep current" * (current is not None) + "): ")
        if value.strip().lower() == "/back":
            return None
        if not value.strip() and current is not None:
            return current
        try:
            return ReviewAnswer(question_id=question.question_id, answer=value)
        except ValidationError:
            say("Please enter an answer between 1 and 1,600 characters, or /back.")


def collect_answers(review, answers: dict) -> list[ReviewAnswer] | None:
    """Keep drafts in this terminal and allow corrections before a single submission."""
    for number, question in enumerate(review.questions, 1):
        if question.question_id not in answers:
            answer = ask_answer(question, number, len(review.questions))
            if answer is None:
                return None
            answers[question.question_id] = answer
    while True:
        say("\nYour answers:")
        for number, question in enumerate(review.questions, 1):
            say(f"  {number}. {question.question}\n     {answers[question.question_id].answer}")
        choice = choose("Ready to continue?", ["Submit answers and fetch sources", "Edit an answer", "Back to review menu"])
        if choice == 0:
            return [answers[q.question_id] for q in review.questions]
        if choice == 2:
            return None
        index = choose("Which answer?", [q.question for q in review.questions] + ["Back"])
        if index < len(review.questions):
            question = review.questions[index]
            answer = ask_answer(question, index + 1, len(review.questions), answers[question.question_id])
            if answer is not None:
                answers[question.question_id] = answer


def interactive_plan_review(output_root: Path, run_id: str, *, on_event=None):
    """Read the current review, collect a decision, then use the existing resume service.

    No worker lock is held while asking questions. Submission revalidates the
    exact review ID; another terminal resolving it first cannot cause a stale
    answer here to approve a new plan or fetch again.
    """
    review = get_plan_review(output_root, run_id)
    answers = {}
    try:
        show_review(review)
        while True:
            choice = choose("\nWhat would you like to do?", ["Answer the questions", "Proceed with the listed assumptions",
                           "Reject this plan", "Leave paused for later", "Show the full plan"])
            if choice == 4:
                show_review(review, expanded=True)
                continue
            if choice == 3:
                raise ReviewDeferred()
            if choice == 0:
                response = collect_answers(review, answers)
                if response is None:
                    continue
                decision = PlanReviewDecision(run_id=run_id, review_id=review.review_id, action="answer", answers=response)
            elif choice == 1:
                if not confirm("Proceed without answering these questions?"):
                    continue
                decision = PlanReviewDecision(run_id=run_id, review_id=review.review_id, action="accept_assumptions")
            else:
                if not confirm("Reject this plan and end the run without fetching?"):
                    continue
                decision = PlanReviewDecision(run_id=run_id, review_id=review.review_id, action="reject")
            break
    except (KeyboardInterrupt, EOFError):
        # Only terminal input is covered here. Interrupts during resumed fetching
        # must retain the workflow's actual cancellation semantics.
        say()
        raise ReviewDeferred(interrupted=True) from None
    say("\nSubmitting your decision…")
    return submit_plan_review(output_root, decision, on_event=on_event)


def new_research_arguments(output_root: Path) -> list[str]:
    """Collect question/source references and return arguments for the normal CLI.

    The simple menu uses default planning/retrieval behavior; it does not yet
    offer extraction or budget controls. Use explicit --extract/--max-requests
    flags at launch for that path. No source document is read by this setup form.
    """
    while True:
        question = input("\nWhat would you like to research? ").strip()
        try:
            ResearchRequest(question=question)
            break
        except ValidationError:
            say("Please enter a research question between 1 and 4,000 characters.")
    while True:
        urls = input("URL list file (Enter for none): ").strip()
        documents = []
        say("Add local document paths one at a time. Press Enter when finished.")
        while len(documents) < 25:
            document = input("Document path (Enter to finish): ").strip()
            if not document:
                break
            documents.append(str(Path(document).expanduser()))
        urls = str(Path(urls).expanduser()) if urls else None
        try:
            if not urls and not documents:
                raise ResearchError("Add a URL list or document so there is something to fetch.")
            load_sources(urls, documents)
            break
        except ResearchError as exc:
            say(f"Sources need attention: {exc} Please enter the sources again.")
    arguments = ["research", "--question", question, "--output-dir", str(output_root)]
    if urls:
        arguments.extend(["--urls", urls])
    for document in documents:
        arguments.extend(["--document", document])
    say("\nStarting research with the configured default model and limits.")
    return arguments


def launch_research_menu(output_root: Path) -> list[str] | None:
    """Choose a normal CLI action, including saved reviews by title rather than ID."""
    if not terminal_available():
        raise ResearchError("The guided menu needs a terminal. Use research/review flags for scripts.")
    say("\nProjectClarity")
    while True:
        choice = choose("What would you like to do?", ["Start new research", "Continue a paused review", "Quit"])
        if choice == 2:
            return None
        if choice == 0:
            return new_research_arguments(output_root)
        reviews = list_pending_plan_reviews(output_root)
        if not reviews:
            say("No resumable plan reviews found in this output folder. Older runs without checkpoints cannot resume.")
            continue
        index = choose("Choose a paused research plan:", [f"{review.created_at[:16]} — {review.plan.objective}" for review in reviews] + ["Back"])
        if index < len(reviews):
            return ["review", reviews[index].run_id, "--output-dir", str(output_root)]
