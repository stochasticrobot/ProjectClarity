"""Translate terminal commands into a research request and display the result.

Purpose:
    Read the command you type, run the requested research steps, and show results.
    For example, ``research --question "Who researches AI security in Canada?"
    --urls source_urls.txt --document report.pdf`` provides a question, a list of web
    addresses, and a local document reference. This file turns those options into
    one ResearchRequest and later prints a short outcome and saved locations.
    --details adds the full plan, graph and extraction summary; --show-prompts
    displays actual model messages/replies while calls run.
    The same flags work without the word ``research``: ``python -m projectclarity
    --question "..." --urls source_urls.txt`` starts immediately, skipping setup.
Responsibilities:
    Parse the research command; validate question/budget options with the shared
    contracts; call agentic_workflow.run_research(); display outcomes and saved paths;
    convert input, execution, and cancellation outcomes into shell exit codes.
    Show readable live progress on stderr and expose status/watch/cancel commands. The graph
    command prints the connected steps recorded for a saved run. These inspection
    commands do not rerun planning. inspect-extraction additionally reads private
    prompts/replies (or saved source text to reconstruct older inputs) and writes
    a readable report, without making another provider call.
Typical use:
    __main__.py calls main() for ``python -m projectclarity research ...``.
    main(argv) also accepts an explicit argument list for callers and tests.
Boundary:
    By default a valid, unambiguous plan leads to reading the supplied sources.
    --plan-only stops before that reading. Clarification requests pause the graph;
    a real terminal then offers guided review automatically. The graph never
    calls input() itself. With no arguments, the terminal opens a small menu.
    Supplying research flags skips only that menu/setup, not plan review.
    --extract adds source-grounded candidate extraction and Python evidence
    checks. --review-claims adds semantic review/adjudication; --follow-links adds
    one bounded discovery hop. Web search and final reports remain deferred.
"""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from projectclarity.research_schemas import DEFAULT_MODEL, Budget, FetchLimits, ResearchRequest
from projectclarity.research_planner import ResearchError
from projectclarity.plan_renderer import render_plan, terminal_text
from projectclarity.agentic_workflow import run_research
from projectclarity.run_monitor import console_progress
from projectclarity.terminal_run_progress import ResearchConsole
from projectclarity.terminal_run_summary import render_run_summary
from projectclarity.providers.model_call_inspection import inspect_calls
from projectclarity.run_status_commands import request_cancel, show_status
from projectclarity.run_graph_visualization import export_saved_run_graph
from projectclarity.extraction.extraction_inspection import inspect_extraction, render_extraction_summary
from projectclarity.plan_review_cli import add_review_command, run_review_command
from projectclarity.terminal_research_interface import (ReviewDeferred, interactive_plan_review,
    launch_research_menu, terminal_available, use_interactive_review)


def argument_parser() -> argparse.ArgumentParser:
    """Describe supported commands and options; parsing alone performs no research."""
    parser = argparse.ArgumentParser(prog="projectclarity", description="Plan research, read sources, and optionally extract/review claims and follow useful links.",
        epilog='Quick start: projectclarity --question "Your research question" --urls source_urls.txt. '
               'Add --document PATH for local sources (repeat as needed). '
               'No arguments opens the menu. Use research --help for all research options.')
    commands = parser.add_subparsers(dest="command", required=True)
    add_review_command(commands)
    graph_command = commands.add_parser("graph", help="Print a saved run's text graph; no research/model call")
    graph_command.add_argument("run_id", help="Run ID printed when research starts")
    graph_command.add_argument("--output-dir", type=Path, default=Path(".projectclarity/runs"))
    inspection = commands.add_parser("inspect-extraction", help="Show saved extraction prompts/replies; no model call")
    inspection.add_argument("run_id", help="Run ID printed when research starts")
    inspection.add_argument("--call", dest="call_id", help="Show a complete call, e.g. S1_P1; otherwise summarize all calls")
    inspection.add_argument("--output-dir", type=Path, default=Path(".projectclarity/runs"))
    call_inspection = commands.add_parser("inspect-calls", help="Read saved claim-review/discovery calls; no model request")
    call_inspection.add_argument("run_id")
    call_inspection.add_argument("--call", dest="call_id", help="For example review_S1_P1 or discovery_hop1")
    call_inspection.add_argument("--output-dir", type=Path, default=Path(".projectclarity/runs"))
    interactive = commands.add_parser("interactive", help="Guided menu: start research or continue a paused review")
    interactive.add_argument("--output-dir", type=Path, default=Path(".projectclarity/runs"))
    research = commands.add_parser("research", help="Plan and read supplied sources; optionally extract/review claims and follow one link hop")
    research.add_argument("--question", required=True, help="The research question, preserved exactly")
    research.add_argument("--urls", help="UTF-8 file with one HTTP(S) URL per line; optional # comments")
    research.add_argument("--document", action="append", default=[], help="Local text, HTML, or PDF path; repeat for multiple files. Relative to the launch directory")
    research.add_argument("--plan-only", action="store_true", help="Stop after planning: do not fetch URLs or read document contents")
    research.add_argument("--extract", action="store_true", help="Send retrieved text to the LLM for candidate extraction; requires --max-requests >= 2; no accepted publication")
    research.add_argument("--review-claims", action="store_true", help="Independently review and adjudicate extracted claims; requires --extract and --max-requests >= 3")
    research.add_argument("--follow-links", action="store_true", help="Permit one follow-up hop on supplied URL origins; requires --review-claims and --max-requests >= 6")
    research.add_argument("--max-followup-sources", type=int, default=2, help="Maximum new first-hop pages (1–3; default 2); shares all run budgets")
    research.add_argument("--non-interactive", action="store_true", help="Do not prompt for plan review, even in a terminal")
    research.add_argument("--model", default=DEFAULT_MODEL, help="Anthropic model ID; no automatic fallback")
    research.add_argument("--max-requests", type=int, default=1, help="Total planning/extraction/review/discovery request ceiling (0–30; default 1, no extraction)")
    research.add_argument("--max-output-tokens", type=int, default=2048, help="Output cap per request (128–8192; default 2048)")
    research.add_argument("--output-dir", type=Path, default=Path(".projectclarity/runs"), help="Local session directory (default: .projectclarity/runs)")
    research.add_argument("--max-fetch-seconds", type=float, default=180.0, help="Retrieval elapsed-time allowance, checked between operations (0–180 seconds, exclusive of zero)")
    research.add_argument("--fetch-timeout-seconds", type=float, default=10.0, help="Per-HTTP connect/read timeout (greater than 0, at most 10 seconds)")
    research.add_argument("--max-source-bytes", type=int, default=5 * 1024 * 1024, help="Per-source byte cap (1–5242880)")
    research.add_argument("--max-total-bytes", type=int, default=25 * 1024 * 1024, help="Total acquisition byte cap, including robots (1–26214400)")
    research.add_argument("--max-http-requests", type=int, default=50, help="HTTP attempt cap, including robots/redirects (0–50)")
    progress = research.add_mutually_exclusive_group()
    progress.add_argument("--quiet-progress", action="store_true", help="Disable live stderr progress; structured files are still saved")
    progress.add_argument("--progress-json", action="store_true", help="Write live progress events as JSON lines to stderr")
    progress.add_argument("--show-prompts", action="store_true", help="Show actual private model messages and replies as calls run; includes source passages")
    research.add_argument("--details", action="store_true", help="Also print the full saved graph, plan and extraction summary at the end")
    for name, help_text in [("status", "Inspect a run without reading source text or contacting the LLM"),
                            ("cancel", "Request a graceful stop of a local running worker")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("run_id", help="Run ID printed when research starts")
        command.add_argument("--output-dir", type=Path, default=Path(".projectclarity/runs"))
        if name == "status":
            command.add_argument("--watch", action="store_true", help="Refresh once per second until finished or interrupted")
            command.add_argument("--json", action="store_true", help="Print machine-readable JSON; --watch produces JSON lines")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch a command and return its shell exit code instead of exiting Python.

    Inspection branches return before any research is started. Research flags
    enter the shared graph; interactive review can resume its same paused run.
    Input errors are shown without dumping provider exception bodies. Argument
    parsing itself may still raise SystemExit, as normal for argparse.
    """
    parser = argument_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        if not terminal_available():
            parser.print_help()
            return 2
        arguments = ["interactive"]
    elif arguments[0].startswith("-") and arguments[0] not in {"-h", "--help"}:
        # Main-entry flags are shorthand for the existing research command.
        # Forward the argument list unchanged: argparse still validates it, and
        # the same graph, budgets, source checks and interactive review run below.
        # Root help stays root help; explicit subcommands keep their own options.
        arguments.insert(0, "research")
    args = parser.parse_args(arguments)
    try:
        if args.command == "inspect-calls":
            print(terminal_text(inspect_calls(args.output_dir, args.run_id, args.call_id)))
            return 0
        if args.command == "inspect-extraction":
            output, destination = inspect_extraction(args.output_dir, args.run_id, args.call_id)
            print(terminal_text(output))
            print(f"Saved private inspection: {terminal_text(str(destination.resolve()))}")
            if not args.call_id:
                print("Add --call S1_P1 to print that call's prompt and reply in full. Use the same --output-dir if customized.")
            return 0
        if args.command == "graph":
            destination = export_saved_run_graph(args.output_dir, args.run_id)
            print(terminal_text(destination.read_text(encoding="utf-8")), end="")
            print(f"Saved graph: {terminal_text(str(destination.resolve()))}")
            return 0
        if args.command == "interactive":
            selected = launch_research_menu(args.output_dir)
            return main(selected) if selected is not None else 0
        if args.command == "status":
            return show_status(args.output_dir, args.run_id, watch=args.watch, json_output=args.json)
        if args.command == "cancel":
            print(request_cancel(args.output_dir, args.run_id), flush=True)
            return 0
        if args.command == "review":
            session = run_review_command(args, on_event=progress_sink(args))
            return display_session_result(session, args.output_dir, details=args.details) if session is not None else 0
        # Convert untrusted CLI input to the typed request before entering the
        # graph. The original question remains separate from the LLM's objective.
        request = ResearchRequest(
            question=args.question,
            urls_file=args.urls,
            document_paths=args.document,
            plan_only=args.plan_only,
            extract_knowledge=args.extract,
            review_claims=args.review_claims,
            follow_links=args.follow_links,
            max_followup_sources=args.max_followup_sources,
            model=args.model,
            budget=Budget(max_requests=args.max_requests, max_output_tokens=args.max_output_tokens),
            fetch_limits=FetchLimits(max_run_seconds=args.max_fetch_seconds,
                request_timeout_seconds=args.fetch_timeout_seconds, max_source_bytes=args.max_source_bytes,
                max_total_bytes=args.max_total_bytes, max_http_requests=args.max_http_requests),
        )
        if request.plan_only:
            print("Planning only — no sources will be fetched or searches executed.", flush=True)
            print("Document references are checked locally; document contents are not read.", flush=True)
        else:
            print("Plan, review if clarification is needed, then retrieve supplied sources. No searches will be executed.", flush=True)
            if request.extract_knowledge:
                print(f"Extraction enabled: retrieved text goes to the LLM; total request cap {request.budget.max_requests} including all model tasks. Candidates are never published.", flush=True)
            if request.review_claims:
                print("Claim review enabled: a separate model task checks support; Python adjudicates statuses.", flush=True)
            if request.follow_links:
                print(f"Discovery enabled: one hop, up to {request.max_followup_sources} extra pages on supplied URL origins. Redirects cannot expand that scope.", flush=True)
        print(f"Model: {terminal_text(request.model)} | output cap: {request.budget.max_output_tokens} tokens | retries: 0", flush=True)
        progress_event = progress_sink(args)

        session = run_research(request, args.output_dir, on_event=None if args.quiet_progress else progress_event)
        if session.status == "awaiting_review" and use_interactive_review(args):
            session = interactive_plan_review(args.output_dir, session.session_id,
                on_event=None if args.quiet_progress else progress_event)
    except ReviewDeferred as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except ValidationError as exc:
        # Field names explain bad CLI input without dumping its original values.
        # Cross-field rules have no field location; explain our two known safe
        # rules rather than printing an empty location or arbitrary error bodies.
        extraction_errors = {"review_budget": "--review-claims requires --extract and --max-requests >= 3.",
            "discovery_budget": "--follow-links requires --review-claims and --max-requests >= 6.","extraction_plan_only": "--extract cannot be combined with --plan-only.",
            "extraction_budget": "--extract needs --max-requests of at least 2 (planning plus extraction)."}
        for error in exc.errors():
            if error["type"] in extraction_errors:
                print(extraction_errors[error["type"]], file=sys.stderr)
                return 2
        fields = ", ".join(".".join(str(item) for item in error["loc"]) for error in exc.errors())
        print(f"Invalid input: {fields}. Check {args.command} --help for limits.", file=sys.stderr)
        return 2
    except ResearchError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Unable to complete the run: {type(exc).__name__}. Check local files and permissions.", file=sys.stderr)
        return 1
    return display_session_result(session, args.output_dir, details=args.details)


def progress_sink(args):
    """Choose one display mode without putting private prompts into JSON event streams."""
    if args.quiet_progress:
        return None
    if args.progress_json:
        return lambda event: console_progress(event, json_output=True)
    return ResearchConsole(args.output_dir, show_prompts=args.show_prompts)


def display_session_result(session, output_root: Path, *, details=False) -> int:
    """Show a short outcome, or full graph/plan details when explicitly requested.

    Distinguish finished, partial, paused and cancelled runs. Both new research
    and review continuation use this display. Candidate
    summaries are private human-facing output, not the content-free event log;
    a successful exit means the configured work finished, not that claims are true.
    """
    directory = output_root / session.session_id
    if details and (directory / "workflow_graph.txt").is_file():
        print(terminal_text((directory / "workflow_graph.txt").read_text(encoding="utf-8")))
    if session.plan is None:
        print(session.error or "No plan was produced.", file=sys.stderr)
        print(f"Session details: {terminal_text(str(directory / 'session.json'))}", file=sys.stderr)
        return 130 if session.status == "cancelled" else 1
    if details:
        print()
        print(terminal_text(render_plan(session)))
        print(f"Saved plan: {terminal_text(str(directory / 'plan.md'))}")
        print(f"Structured plan: {terminal_text(str(directory / 'plan.json'))}")
        print(f"Session details: {terminal_text(str(directory / 'session.json'))}")
        if "retrieve_sources" in session.visited_nodes:
            print(f"Source receipts: {terminal_text(str(directory / 'documents.json'))}")
        if "persist_candidates" in session.visited_nodes:
            print(terminal_text(render_extraction_summary(session)))
            print(f"Candidate claims and passages: {terminal_text(str(directory / 'candidates.md'))}")
            print(f"Extraction receipts: {terminal_text(str(directory / 'extraction.json'))}")
        if session.request.review_claims and session.extraction:
            from collections import Counter
            counts = Counter(c.get("adjudication_status", "pending") for c in session.extraction["candidates"])
            print("Claim adjudication (not publication): " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no candidates"))
            print(f"Review assessments: {terminal_text(str(directory / 'claim_reviews.json'))}")
            print(f"Inspect prompts/replies: python -m projectclarity inspect-calls {session.session_id}")
        if session.discovery:
            print(f"Discovery: {terminal_text(session.discovery.get('stop_reason') or 'unknown')}; "
                  f"follow-up sources={len(session.discovery.get('selected_sources', []))}")
            print(f"Link inventory and decisions: {terminal_text(str(directory / 'discovery.json'))}")
    else:
        print(terminal_text(render_run_summary(session, output_root)))
    if session.error:
        print(session.error, file=sys.stderr)
    if session.status == "cancelled":
        return 130
    if session.status in {"retrieval_partial", "retrieval_failed", "extraction_partial", "extraction_failed"}:
        return 1
    if session.status == "awaiting_review":
        print("Retrieval has not started. This run is paused for plan review; no worker needs to stay running.")
        print(f"Run: {session.session_id} | Review: {session.pending_review['review_id']}")
        print(f"Next: python -m projectclarity review {session.session_id}")
        print(f"Use the same --output-dir if customized. Current output folder: {terminal_text(str(output_root))}")
        return 2
    if not session.request.plan_only and session.status == "needs_clarification":
        print("Retrieval has not started. Resolve the listed questions in --question and start a new run.")
        return 2
    return 0
