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
    --extract performs answer-focused candidate extraction and Python evidence checks.
    --knowledge-max-requests opts into auxiliary broad graph capture within its own
    allowance and the unchanged overall ceiling. The knowledge command reads
    graph records/change events; graph shows workflow steps, not these entities.
    --review-claims adds semantic review/adjudication of answer candidates; --follow-links adds
    recursive budget-bounded discovery. --search adds question-driven search and source
    selection; --assess-coverage adds evidence-linked progress checks. At completion,
    an extractive provisional answer prints reviewed findings without another model
    call. The answer command can inspect older runs; synthesized final reports remain deferred.
    Research commands load config/blacklist.txt and config/whitelist.json by default,
    snapshot domain rules into the request and select a configured search provider.
    Whitelist domains restrict acquisition only with --whitelist-only; catalogue
    seed URLs are added only with --use-default-sources. Neither flag is implied
    by --search. Cross-site link exploration has no depth cap unless requested.
"""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from projectclarity.research_schemas import DEFAULT_MODEL, Budget, FetchLimits, ResearchRequest
from projectclarity.nodes.plan_research.research_planner import ResearchError
from projectclarity.plan_renderer import render_plan, terminal_text
from projectclarity.agentic_workflow import run_research, resume_research
from projectclarity.run_monitor import console_progress
from projectclarity.terminal_run_progress import ResearchConsole
from projectclarity.terminal_run_summary import render_run_summary
from projectclarity.provisional_answer import build_provisional_answer, render_provisional_answer, read_saved_answer
from projectclarity.providers.model_call_inspection import inspect_calls
from projectclarity.knowledge.graph_store import read_knowledge
from projectclarity.discovery.url_policy import CONFIG_DIRECTORY, load_url_configuration, url_denial
from projectclarity.run_status_commands import request_cancel, show_status
from projectclarity.run_graph_visualization import export_saved_run_graph
from projectclarity.extraction.extraction_inspection import inspect_extraction, render_extraction_summary
from projectclarity.plan_review_cli import add_review_command, run_review_command
from projectclarity.terminal_research_interface import (ReviewDeferred, interactive_plan_review,
    launch_research_menu, terminal_available, use_interactive_review)


def argument_parser() -> argparse.ArgumentParser:
    """Describe supported commands and options; parsing alone performs no research.

    Args:
        None.

    Returns:
        An ArgumentParser describing the supported commands and flags; no arguments have
        been parsed yet.
    """
    parser = argparse.ArgumentParser(prog="projectclarity", description="Research questions with saved evidence, optional search/link exploration, and a provisional answer at completion.",
        epilog='Quick start: projectclarity --question "Your research question" --urls source_urls.txt. '
               'Add --document PATH for local sources (repeat as needed). '
               'No arguments opens the menu. Use research --help for all research options.')
    commands = parser.add_subparsers(dest="command", required=True)
    add_review_command(commands)
    answer = commands.add_parser('answer', help='Print a saved run\'s provisional answer; no model call or writes')
    answer.add_argument('run_id')
    answer.add_argument('--output-dir', type=Path, default=Path('.projectclarity/runs'))
    answer.add_argument('--details', action='store_true', help='Include every unresolved lead as well as findings')
    knowledge = commands.add_parser("knowledge", help="Read accumulated entity graph or committed changes; no model call")
    knowledge.add_argument("--output-dir", type=Path, default=Path(".projectclarity/runs"))
    knowledge.add_argument("--since", type=int, help="Return changes after this exclusive cursor (0 starts the feed)")
    knowledge.add_argument("--limit", type=int, default=1000, help="Maximum records per collection or event page (1–10000)")
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
    resume = commands.add_parser('resume', help='Continue remaining journaled work without replanning or resetting budgets')
    resume.add_argument('run_id')
    resume.add_argument('--output-dir', type=Path, default=Path('.projectclarity/runs'))
    resume.add_argument('--details', action='store_true')
    resume.add_argument('--quiet-progress', action='store_true')
    resume.add_argument('--progress-json', action='store_true')
    resume.add_argument('--show-prompts', action='store_true')
    interactive = commands.add_parser("interactive", help="Guided menu: start research or continue a paused review")
    interactive.add_argument("--output-dir", type=Path, default=Path(".projectclarity/runs"))
    research = commands.add_parser("research", help="Research supplied/discovered sources with optional recursive link exploration")
    research.add_argument("--question", required=True, help="The research question, preserved exactly")
    research.add_argument("--urls", help="UTF-8 file with one HTTP(S) URL per line; optional # comments")
    research.add_argument("--document", action="append", default=[], help="Local text, HTML, or PDF path; repeat for multiple files. Relative to the launch directory")
    research.add_argument("--plan-only", action="store_true", help="Stop after planning: do not fetch URLs or read document contents")
    research.add_argument("--extract", action="store_true", help="Extract goal-linked answer candidates; broad graph capture requires --knowledge-max-requests")
    research.add_argument("--no-knowledge", action="store_true", help="Explicitly disable optional broad capture (the new-run default)")
    research.add_argument("--knowledge-max-requests", type=int, default=0, help="Opt into broad knowledge capture with this cumulative model-call allowance (default 0); included within --max-requests, not added to it")
    research.add_argument("--knowledge-calls-per-round", type=int, default=4, help="Capture calls per scheduling turn (default 4); unfinished windows remain queued, not discarded")
    research.add_argument("--review-claims", action="store_true", help="Independently review and adjudicate extracted claims; requires --extract and --max-requests >= 3")
    research.add_argument("--follow-links", action="store_true", help="Follow observed links recursively across public sites; requires --review-claims")
    research.add_argument("--max-followup-sources", type=int, default=5, help="New pages per expansion round (positive integer; default 5); shares all run budgets")
    research.add_argument("--max-link-depth", type=int, default=None, help="Optional link-depth cap; omitted means no depth cap within run budgets")
    research.add_argument("--max-unproductive-depth", type=int, default=3, help="Stop a branch after N fully analyzed pages without new goal-relevant progress (default 3; 0 disables; seeds are neutral)")
    research.add_argument("--explore-before-search-depth", type=int, default=2, help="Explore N relative link levels before releasing search (default 2; 0 disables); blocked/empty frontiers release earlier. Depth does not cap breadth/calls or guarantee search before budget exhaustion")
    research.add_argument("--same-origin-links", action="store_true", help="Restrict followed links to seed origins; off by default")
    research.add_argument("--max-sources", "--max-pages", dest="max_sources", type=int, default=250, help="Registered page/document ceiling, NOT websites (1–1000; default 250)")
    research.add_argument("--blacklist", type=Path, default=CONFIG_DIRECTORY / "blacklist.txt", help="Blocked domain file; deny overrides allow")
    research.add_argument("--whitelist", type=Path, default=CONFIG_DIRECTORY / "whitelist.json", help="Domain/provider/default-source catalogue")
    research.add_argument("--whitelist-only", action="store_true", help="Only fetch whitelist domains; off by default")
    research.add_argument("--use-default-sources", action="store_true", help="Add the whitelist catalogue's seed URLs to this run")
    from projectclarity.discovery.search_engines import SUPPORTED_SEARCH_PROVIDERS
    research.add_argument("--search-provider", choices=sorted(SUPPORTED_SEARCH_PROVIDERS), help="First free engine; default from catalogue")
    research.add_argument("--search-fallbacks", default=None, help="Comma-separated fallback engines; default is remaining catalogue order; 'none' disables fallback")
    research.add_argument("--search-cooldown-seconds", type=float, default=300, help="Initial failed-engine cooldown; doubles on failure (default 300 seconds)")
    research.add_argument("--search-recovery-rounds", type=int, default=2, help="Additional recovery rounds per query/engine (0–3, default 2)")
    research.add_argument("--search-min-interval-seconds", type=float, default=3.0, help="Minimum pause after a provider attempt before another (default 3 seconds)")
    research.add_argument("--search-retries", type=int, default=1, help="Immediate transient retries in single-engine mode (0–3, default 1); multi-engine mode tries the next engine")
    research.add_argument("--search", action="store_true", help="Permit real web search and source selection, including without supplied URLs; requires --max-requests >= 2")
    research.add_argument("--assess-coverage", action="store_true", help="Assess plan criteria against gathered evidence; requires --extract; also enabled by --search with --extract")
    research.add_argument("--max-search-queries", type=int, default=None, help="Optional cumulative query-attempt cap; omitted uses remaining proposals and shared run budgets (two queries per round)")
    research.add_argument("--max-search-sources", type=int, default=None, help="Optional cumulative search-selected-page cap (1–1000); omitted shares --max-sources")
    research.add_argument("--search-results-per-query", type=int, default=5, help="Search results retained per query (1–10; default 5)")
    research.add_argument("--non-interactive", action="store_true", help="Do not prompt for plan review, even in a terminal")
    research.add_argument("--model", default=DEFAULT_MODEL, help="Anthropic model ID; no automatic fallback")
    research.add_argument("--max-requests", type=int, default=None, help="Total model-request ceiling (0–1000; default 100 with --extract or --search, otherwise 1)")
    research.add_argument("--max-output-tokens", type=int, default=None, help="Output cap per request (128–8192; default 8192 with --extract, otherwise 2048)")
    research.add_argument("--output-dir", type=Path, default=Path(".projectclarity/runs"), help="Local session directory (default: .projectclarity/runs)")
    research.add_argument("--max-fetch-seconds", type=float, default=1800.0, help="Shared acquisition wall time from first search/retrieval, including intervening model work (>0 to 7200 seconds; default 1800)")
    research.add_argument("--fetch-timeout-seconds", type=float, default=10.0, help="Per-HTTP connect/read timeout (greater than 0, at most 10 seconds)")
    research.add_argument("--max-source-bytes", type=int, default=5 * 1024 * 1024, help="Per-source byte cap (1–5242880)")
    research.add_argument("--max-total-bytes", type=int, default=250 * 1024 * 1024, help="Total download byte allowance, including robots (default 250 MiB; maximum 1 GiB)")
    research.add_argument("--max-http-requests", type=int, default=1000, help="HTTP attempt allowance, including robots/redirects/retries (default 1000; maximum 10000)")
    progress = research.add_mutually_exclusive_group()
    progress.add_argument("--quiet-progress", action="store_true", help="Disable live stderr progress; final summary/answer still print and files are saved")
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

    Inspection branches return before any research is started. Research flags enter the
    shared graph; interactive review can resume its same paused run. Input errors are shown
    without dumping provider exception bodies. Argument parsing itself may still raise
    SystemExit, as normal for argparse.

    Args:
        argv: Command-line argument strings excluding the program name; None reads
            sys.argv[1:].

    Returns:
        Shell exit code: 0 for completed work, 1 for failure, 2 for input/pending-review
        outcomes or 130 for cancellation. Argument parsing may instead raise SystemExit.
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
        if args.command == 'answer':
            print(render_provisional_answer(read_saved_answer(args.output_dir, args.run_id),
                include_leads=args.details), end='')
            return 0
        if args.command == "knowledge":
            import json
            print(json.dumps(read_knowledge(args.output_dir / "knowledge.sqlite3", since=args.since, limit=args.limit), indent=2, ensure_ascii=True))
            return 0
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
        if args.command == 'resume':
            session = resume_research(args.run_id, args.output_dir, on_event=progress_sink(args))
            return display_session_result(session, args.output_dir, details=args.details)
        if args.command == "review":
            session = run_review_command(args, on_event=progress_sink(args))
            return display_session_result(session, args.output_dir, details=args.details) if session is not None else 0
        # Convert untrusted CLI input to the typed request before entering the
        # graph. The original question remains separate from the LLM's objective.
        try:
            policy, catalogue = load_url_configuration(args.blacklist, args.whitelist, whitelist_only=args.whitelist_only)
        except (OSError, ValueError) as exc:
            raise ResearchError('Cannot load URL policy configuration: ' + terminal_text(str(exc))) from None
        search_provider = args.search_provider or catalogue['default_search_provider']
        if search_provider not in catalogue['search_providers']:
            raise ResearchError("Search provider is not enabled in the whitelist catalogue")
        fallbacks = ([p for p in catalogue['search_providers'] if p != search_provider]
            if args.search_fallbacks is None else [] if args.search_fallbacks.lower() == 'none'
            else list(dict.fromkeys(p.strip() for p in args.search_fallbacks.split(','))))
        if any(p not in catalogue['search_providers'] for p in fallbacks):
            raise ResearchError("Every fallback engine must be enabled in the whitelist catalogue")
        # Provider permission is separate from source-domain permission: an
        # allow-only research scope must still be able to query its configured
        # engine. Check the provider's named domain against deny rules only;
        # this is not interception of all internal DDGS network requests.
        from projectclarity.discovery.search_engines import PROVIDER_HOSTS
        if args.search and any(url_denial('https://' + PROVIDER_HOSTS[p],
                policy.model_copy(update={'whitelist_only': False})) for p in [search_provider, *fallbacks]):
            raise ResearchError("Selected search provider or fallback is blocked by the domain blacklist")
        if args.no_knowledge and args.knowledge_max_requests:
            raise ResearchError('--no-knowledge conflicts with --knowledge-max-requests')
        if args.knowledge_max_requests and not args.extract:
            raise ResearchError('--knowledge-max-requests requires --extract')
        request = ResearchRequest(
            question=args.question,
            urls_file=args.urls,
            document_paths=args.document,
            plan_only=args.plan_only,
            extract_knowledge=args.extract,
            accumulate_knowledge=args.extract and args.knowledge_max_requests > 0,
            knowledge_max_requests=args.knowledge_max_requests,
            knowledge_calls_per_round=args.knowledge_calls_per_round if args.search or args.follow_links else None,
            review_claims=args.review_claims,
            follow_links=args.follow_links,
            max_followup_sources=args.max_followup_sources,
            max_link_depth=args.max_link_depth, same_origin_links=args.same_origin_links,
            max_unproductive_depth=args.max_unproductive_depth if args.max_unproductive_depth != 0 else None,
            explore_before_search_depth=args.explore_before_search_depth or None,
            max_sources=args.max_sources, url_policy=policy, search_provider=search_provider,
            search_fallbacks=fallbacks, search_cooldown_seconds=args.search_cooldown_seconds,
            search_recovery_rounds=args.search_recovery_rounds,
            search_min_interval_seconds=args.search_min_interval_seconds,
            search_retries=args.search_retries,
            default_source_urls=catalogue['default_sources'] if args.use_default_sources else [],
            web_search=args.search,
            assess_coverage=args.assess_coverage or (args.search and args.extract),
            max_search_queries=args.max_search_queries,
            max_search_sources=args.max_search_sources,
            search_results_per_query=args.search_results_per_query,
            model=args.model,
            budget=Budget(max_requests=args.max_requests if args.max_requests is not None else
                          (100 if (args.extract or args.search) and not args.plan_only else 1),
                          max_output_tokens=args.max_output_tokens if args.max_output_tokens is not None else
                          (8192 if args.extract else 2048)),
            fetch_limits=FetchLimits(max_run_seconds=args.max_fetch_seconds,
                request_timeout_seconds=args.fetch_timeout_seconds, max_source_bytes=args.max_source_bytes,
                max_total_bytes=args.max_total_bytes, max_http_requests=args.max_http_requests),
        )
        if request.plan_only:
            print("Planning only — no sources will be fetched or searches executed.", flush=True)
            print("Document references are checked locally; document contents are not read.", flush=True)
        else:
            print("Plan, clarify if needed, search the web and retrieve selected sources." if request.web_search else
                  "Plan, review if clarification is needed, then retrieve supplied sources. No searches will be executed.", flush=True)
            if request.extract_knowledge:
                print(f"Extraction enabled: retrieved text goes to the LLM; total request cap {request.budget.max_requests} including all model tasks. Candidates are never published.", flush=True)
            if request.accumulate_knowledge:
                print(f"Knowledge capture enabled: entities and observations accumulate in {args.output_dir / 'knowledge.sqlite3'}; relevance remains unassessed.", flush=True)
            if request.review_claims:
                print("Claim review enabled: a separate model task checks support; Python adjudicates statuses.", flush=True)
            if request.follow_links:
                scope = 'seed origins only' if request.same_origin_links else 'public sites subject to URL policy'
                depth = str(request.max_link_depth) if request.max_link_depth is not None else 'no separate depth cap'
                print(f"Discovery enabled: up to {request.max_followup_sources} new pages per round, {request.max_sources} total pages/documents (not websites); {scope}; depth: {depth}.", flush=True)
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
    """Choose one display mode without putting private prompts into JSON event streams.

    Args:
        args: Parsed argparse namespace containing quiet, JSON-progress and private-prompt
            display options.

    Returns:
        A ResearchConsole, JSON-event callback or None, according to the requested display
        mode.
    """
    if args.quiet_progress:
        return None
    if args.progress_json:
        return lambda event: console_progress(event, json_output=True)
    return ResearchConsole(args.output_dir, show_prompts=args.show_prompts)


def display_session_result(session, output_root: Path, *, details=False) -> int:
    """Show a short outcome, or full graph/plan details when explicitly requested.

    Distinguish finished, partial, paused and cancelled runs. Both new research and review
    continuation use this display. Candidate summaries are private human-facing output, not
    the content-free event log; a successful exit means the configured work finished, not
    that claims are true. Print and save an extractive provisional answer even when
    the budget is exhausted; reporting spends no model calls. Answer write/validation
    failures are reported separately and do not change the research exit status.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        details: Whether to print full plan/graph details in addition to the compact run
            summary.

    Returns:
        Shell exit code reflecting execution status, not the truth of candidate claims: 0
        completed, 1 failed, 2 paused or 130 cancelled.
    """
    directory = output_root / session.session_id
    if details and (directory / "workflow_graph.txt").is_file():
        print(terminal_text((directory / "workflow_graph.txt").read_text(encoding="utf-8")))
    if session.plan is None:
        display_provisional_answer(session, output_root)
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
    display_provisional_answer(session, output_root)
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


def display_provisional_answer(session, output_root: Path) -> None:
    """Print the current grounded answer and save full evidence-linked artifacts.

    Args:
        session: Returned run snapshot, including partial, cancelled or empty results.
        output_root: Parent of its existing run directory; no model budget is used.

    Returns:
        None. Print all supported answer statements and counterevidence, save
        answer.md with full leads and answer.json with evidence spans. A validation
        failure prints an explicit unavailable notice; an I/O failure still leaves
        the terminal answer visible. Original source/claim receipts are untouched.
    """
    import json
    from projectclarity.session_storage import write_text

    try:
        answer = build_provisional_answer(session, output_root)
    except (OSError, ValueError):
        print('\nCurrent answer (provisional): unavailable because saved evidence could not be read or validated.', file=sys.stderr)
        return
    print('\n' + render_provisional_answer(answer), end='')
    directory = output_root / session.session_id
    try:
        write_text(directory / 'answer.md', render_provisional_answer(answer, include_leads=True))
        write_text(directory / 'answer.json', json.dumps(answer, indent=2, ensure_ascii=False) + '\n')
    except OSError:
        print('Could not save answer artifacts; the provisional answer is printed above.', file=sys.stderr)
    else:
        print('Saved current answer: ' + terminal_text(str(directory / 'answer.md')))
