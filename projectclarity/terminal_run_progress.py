"""Tell the story of a running research task in an ordinary terminal.

For example: 'Retrieve sources', 'Downloading S1: https://example.org/team',
then 'Saved 3,200 characters'. This is a presentation layer, not a graph node.
RunMonitor sends safe events here; private source labels are read separately
from the run snapshot. They never get added to events.jsonl or status telemetry.
Committed graph additions are read from the local database using cursor ranges;
new nodes, edges and attributes appear without enabling private prompt display.
Budget summaries at event boundaries expose remaining allowances and reported
tokens. The display neither enforces limits nor invents a whole-run token cap.

With --show-prompts, explicit private callbacks also display the actual model
messages and returned answer text. Those callbacks do not write operational logs,
change requests, reconstruct old inputs, or make another model call. Prompts may
contain full document passages. Credentials are redacted and terminal control
characters removed. No Rich/TUI dependency or interactive terminal is required.
"""
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from projectclarity.extraction.extraction_call_tracing import capture_answer, redact_text
from projectclarity.plan_renderer import terminal_text
from projectclarity.knowledge.graph_store import read_committed_additions
from projectclarity.coverage.scope_estimation import scope_lines


NODE_EXPLANATIONS = {
    "web_search": ("Search the web", "Executing bounded queries; search snippets are leads, not supporting evidence."),
    "accumulate_knowledge": ("Capture the knowledge graph", "Recording source-grounded entities, attributes and connections without requiring answer relevance."),
    "schedule_work": ("Choose remaining work", "Checking queued work, retry readiness and shared budgets."),
    "wait_for_retry": ("Wait for a bounded retry", "Other runnable work is exhausted; observing cancellation and the acquisition deadline."),
    "select_sources": ("Select search sources", "Ranking useful observed results, including leads and coverage evidence."),
    "assess_research_gaps": ("Assess research coverage", "Comparing collected evidence with the plan's criteria and identifying useful next searches."),
    "initialize_research": ("Set up research", "Registering your sources and execution limits; not reading document contents yet."),
    "plan_research": ("Plan the research", "Asking the model to turn your question into research goals and a strategy."),
    "review_plan": ("Check the plan", "Checking whether we need your input before collecting sources."),
    "retrieve_sources": ("Retrieve sources", "Downloading permitted pages or reading supplied documents, then saving text and provenance."),
    "extract_knowledge": ("Extract candidate knowledge", "Reading saved passages with the model to identify relevant people, organizations and claims."),
    "validate_evidence": ("Check evidence references", "Checking source versions, identifiers and exact quotations. This does not establish truth."),
    "persist_candidates": ("Save candidate claims", "Saving claims and supporting passages for inspection; nothing is published."),
    "review_claims": ("Review claim support", "Asking a separate model task whether the original passage supports each claim."),
    "adjudicate_claims": ("Assign claim statuses", "Applying Python rules to review results and rechecking the saved evidence."),
    "select_followup_sources": ("Consider follow-up sources", "Choosing the next observed-link frontier; shared destinations are reused, not fetched twice."),
}

REASONS = {
    # Historical saved-run code; new recursive runs use current frontier/depth stops.
    "single_hop_complete": "The permitted follow-up hop is complete; no second hop will run.",
    "link_depth_limit": "The configured link-depth limit was reached; this is not evidence of saturation.",
    "frontier_considered": "All currently offered links have a recorded selection outcome; this is not proof of research completeness.",
    "frontier_selection_exhausted": "The bounded selection attempts are exhausted; unresolved links remain recorded.",
    "no_eligible_links": "No new links meet this run's source permissions.",
    "branch_unproductive_limit": "This branch reached its consecutive no-progress limit; sibling branches remain available.",
    "branch_analysis_pending": "Branch expansion is waiting for usable analysis; unknown is not zero yield.",
    "branch_path_unavailable": "No currently usable path reaches this page; it is not counted as an empty finding.",
    "branch_path_search_limit": "The route-analysis bound was reached; unresolved paths are paused, not declared unproductive.",
    "branch_limit_reached": "Remaining branches reached the no-progress stopping policy; this is not research completeness.",
    "search_exploration_boundary": "This link is beyond the current exploration window; it remains available for a later wave.",
    "no_useful_links_selected": "The selector found no useful follow-up links in the offered list.",
    "request_or_source_limit": "There is not enough remaining request or source allowance for another investigation.",
    "fetch_budget_exhausted": "The shared download time, HTTP-request or byte allowance is exhausted.",
    "followup_registered": "Follow-up sources are registered and queued for permitted retrieval.",
    "frontier_batch_deferred": "No useful link selected in this context batch; considering pending leads next.",
    "frontier_context_blocked": "The frontier cannot fit the current model context; pending leads are retained, not exhausted.",
    "supplied_sources_only": "This run is limited to the sources you supplied.",
    "provider_unavailable": "An earlier model request failed; no further selection request will be sent.",
    "invalid_selection_references": "Invalid link/goal choices were rejected individually; independently valid choices were retained.",
    "request_limit": "The applicable request allowance is exhausted.",
    "knowledge_request_limit": "The optional broad-capture allowance is exhausted; primary research can continue only if overall budget remains.",
    "search_provider_blocked": "The search provider returned a block or challenge. Its saved cooldown and fallback policy govern further attempts; this is not an empty result.",
    "search_rate_limited": "The search provider rate-limited requests; saved cooldown and fallback permissions govern further attempts.",
    "search_response_unrecognized": "The search response contained no usable results and no confirmed empty-result signal.",
    "search_service_unavailable": "The search provider returned a server error; bounded retry receipts are retained.",
    "invalid_extraction_schema": "The extraction reply did not match the required answer format.",
    "invalid_output_schema": "The model reply did not match the required answer format.",
    "incomplete_model_output": "The model did not finish its answer, possibly because of the output-token cap.",
    "review_quote_not_found": "A reviewer quotation could not be matched to the source passage; that review was not accepted.",
    "review_ambiguous_quote": "A reviewer quotation has more than one possible source location; that review was not accepted.",
    "provider_or_adapter_error": "The model request failed; inspect its receipt for the recorded outcome.",
    "cancelled": "Stopped at your cancellation request; earlier work is retained.",
}


def short_text(value, limit=200):
    """Make an untrusted preview one line; explicit prompt mode does not use this shortening.

    Args:
        value: Research-derived value to preview in the compact terminal output.
        limit: Maximum retained text characters before appending an ellipsis when shortened.

    Returns:
        A printable, whitespace-collapsed preview; shortened output contains up to
        limit text characters plus one ellipsis character.
    """
    value = " ".join(terminal_text(str(value)).split())
    return value if len(value) <= limit else value[:limit] + "…"


def explain_reason(code):
    """Translate known stop/failure codes, retaining unfamiliar codes for diagnosis.

    Args:
        code: Application stop/failure code stored in a receipt or progress result.

    Returns:
        A readable explanation for a known code, retaining an unfamiliar code so it remains
        diagnosable.
    """
    return REASONS.get(code, str(code or "No additional detail was recorded.").replace("_", " "))


def source_label(source):
    """Show a useful URL/file label without URL credentials, fragments or query values.

    Args:
        source: Serialized source dictionary with its file path or URL; query values and URL
            credentials are omitted from display.

    Returns:
        A file basename or sanitized URL suitable for compact progress output.
    """
    if source.get("kind") != "url":
        return Path(source.get("path", "document")).name
    parts = urlsplit(source["url"])
    host = parts.hostname or "unknown host"
    if ":" in host:
        host = f"[{host}]"
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "[query omitted]" if parts.query else "", ""))


class ResearchConsole:
    """A per-command event sink; private model display is opt-in and never persisted here."""

    def __init__(self, output_root, *, show_prompts=False):
        """Remember display options only; don't read files or start a research run.

        Args:
            self: Console renderer holding display preferences and the current run identifier.
            output_root: Local filesystem directory containing research-run subdirectories; not
                a URL or an individual run folder.
            show_prompts: Whether the user opted in to showing private application prompts and
                visible model replies.

        Returns:
            None. Initialize display preferences and an inactive retrieval-clock anchor
            without opening files or starting research.
        """
        self.output_root = Path(output_root)
        self.show_prompts = show_prompts
        self.directory = None
        self.step = 0
        self.reported_intersections = set()
        self.retrieval_clock = None

    def say(self, text="", *, secret=None):
        """Write private human-facing text safely, without attaching it to operational events.

        Args:
            self: Console renderer holding display preferences and the current run identifier.
            text: Human-facing progress, prompt or result text to display.
            secret: Optional exact API key to redact in addition to recognizable credential
                patterns; None adds no exact-key replacement.

        Returns:
            None. Print sanitized, credential-redacted private display text.
        """
        safe, _ = redact_text(str(text), secret)
        print(terminal_text(safe), file=sys.stderr, flush=True)

    def session(self):
        """Read the latest inspectable projection; missing snapshots must not fail research.

        Args:
            self: Console renderer holding display preferences and the current run identifier.

        Returns:
            The latest saved session JSON dictionary, or an empty dictionary when it cannot yet
            be read.
        """
        if self.directory is None:
            return {}
        try:
            return json.loads((self.directory / "session.json").read_text())
        except (OSError, ValueError):
            return {}

    def heading(self, node):
        """Identify the actual executing node and explain its job without jargon.

        Args:
            self: Console renderer holding display preferences and the current run identifier.
            node: Registered workflow-node name just announced by execution monitoring.

        Returns:
            None. Print the active node name and a plain-language explanation of its work.
        """
        title, explanation = NODE_EXPLANATIONS.get(node, (node.replace("_", " "), ""))
        self.step += 1
        self.say(f"\nStep {self.step} · {title} [{node}]")
        if explanation:
            self.say("  " + explanation)

    def budget_status(self):
        """Expose remaining allowances without altering research or inventing a token cap.

        Read saved usage and the latest monitor snapshot. Advance the retrieval wall
        clock between events, including model work, only after retrieval has started.
        This is event-driven reporting, not a ticking timer or a new enforcement layer.

        Args:
            self: Console with the active run directory and optional monotonic retrieval
                clock anchor; private snapshots stay local to the human display.

        Returns:
            None. Print consumed/remaining resources, page outcomes and distinct registered
            URL hostnames separately. Missing session data suppresses the display; unknown
            token usage is explicitly unknown.
        """
        session = self.session()
        request = session.get("request", {})
        if not request:
            return
        try:
            snapshot = json.loads((self.directory / "run_status.json").read_text())
        except (OSError, ValueError):
            snapshot = {}
        usage = session.get("usage", {})
        limits = request["fetch_limits"]
        budget = request["budget"]
        retrieval = {**session.get("retrieval_usage", {}), **snapshot.get("metrics", {})}
        calls = usage.get("requests_attempted", 0)
        http = retrieval.get("http_requests", 0)
        byte_count = retrieval.get("bytes_read", 0)
        pages = snapshot.get("sources", session.get("documents", []))
        retrieved = sum(p.get("status") == "retrieved" for p in pages)
        failed = sum(p.get("status") == "failed" for p in pages)
        registered = max(len(session.get("sources", [])), len(pages))
        hosts = {urlsplit(s['url']).hostname for s in session.get('sources', []) if s.get('url')}
        cap = request["max_sources"]
        if self.retrieval_clock is None:
            clock_text = f"not started ({limits['max_run_seconds']:.0f}s allowance)"
        else:
            elapsed, observed_at = self.retrieval_clock
            elapsed += max(0, time.monotonic() - observed_at)
            remaining = max(0, limits["max_run_seconds"] - elapsed)
            clock_text = f"{elapsed:.0f}/{limits['max_run_seconds']:.0f}s ({remaining:.0f}s left)"
        self.say(f"  Budget · retrieval wall time: {clock_text}; model requests: "
            f"{calls}/{budget['max_requests']} ({max(0, budget['max_requests'] - calls)} left).")
        capture = sum(c.get('task') == 'accumulate_knowledge' and c.get('status') in
            {'started', 'completed', 'failed'} for c in session.get('processing_calls', []))
        allowance = request.get('knowledge_max_requests')
        self.say(f"    Research calls: {calls - capture}; broad capture: {capture}/"
            f"{allowance if allowance is not None else 'legacy shared allowance'} (included in total).")
        self.say(f"    Pages/documents registered: {registered}/{cap} ({max(0, cap - registered)} slots left); "
            f"website hostnames: {len(hosts)}; "
            f"pages retrieved: {retrieved}, failed: {failed}; HTTP attempts: "
            f"{http}/{limits['max_http_requests']} ({max(0, limits['max_http_requests'] - http)} left).")
        tokens = usage.get("total_tokens")
        token_text = f"{tokens:,}" if tokens is not None else "unknown/incomplete"
        self.say(f"    Reported tokens: {token_text} (no run-wide token cap; "
            f"{budget['max_output_tokens']:,} output tokens/reply); bytes: "
            f"{byte_count:,}/{limits['max_total_bytes']:,} "
            f"({max(0, limits['max_total_bytes'] - byte_count):,} left).")
        if request.get("web_search"):
            search = session.get("search", {})
            queries = len(search.get("queries", []))
            selected = len(search.get("selected_sources", []))
            search_cap = request.get('max_search_sources')
            shared_note = " (shared overall source allowance)" if search_cap is None else ""
            search_cap = cap if search_cap is None else search_cap
            search_left = min(max(0, search_cap - selected), max(0, cap - registered))
            query_cap = request.get('max_search_queries')
            query_text = (f"{queries}/{query_cap} ({max(0, query_cap - queries)} left)"
                if query_cap is not None else f"{queries} (no separate query cap)")
            self.say(f"    Search queries: {query_text}; "
                f"search-selected sources: {selected}/{search_cap} "
                f"({search_left} left){shared_note}.")

    def model_input(self, task, call_id, messages, schema, secret):
        """Display the very messages about to be invoked, not a separately rebuilt prompt.

        The schema name identifies the separate structured-output contract; its full JSON
        remains in extraction/review/discovery traces. This view shows application messages, not
        hidden provider instructions or HTTP headers.

        Args:
            self: Console renderer holding display preferences and the current run identifier.
            task: Workflow task name used to identify the model call in receipts and displays.
            call_id: Application call identifier selecting one saved model attempt, not a
                filesystem path.
            messages: Ordered (role, text) application messages sent to the model; may contain
                private research content.
            schema: Pydantic response-model class defining the structured answer expected from
                the provider.
            secret: Optional exact API key to redact in addition to recognizable credential
                patterns; None adds no exact-key replacement.

        Returns:
            None. Display actual application messages only when private prompt display is
            enabled.
        """
        if not self.show_prompts:
            return
        self.say(f"\n  --- MODEL INPUT · {task} · {call_id} ---")
        self.say(f"  Expected structured answer: {schema.__name__}")
        for role, content in messages:
            self.say(f"\n  {role.upper()} MESSAGE", secret=secret)
            # Pretty-print task JSON, preserving strings and the complete source
            # passage. No truncation or rewritten research instructions here.
            try:
                payload = json.loads(content)
            except (ValueError, TypeError):
                self.say(content, secret=secret)
                continue
            if isinstance(payload, dict) and isinstance(payload.get("passage"), dict):
                # Keep passage IDs and any other metadata in the JSON display.
                # Move only the text into a readable block; payload is a fresh
                # decoded copy, so the message sent to the provider is unchanged.
                passage = payload["passage"]
                passage_text = passage.pop("text", "")
                self.say(json.dumps(payload, ensure_ascii=False, indent=2), secret=secret)
                self.say(f"\nSOURCE PASSAGE {passage.get('passage_id', '?')}", secret=secret)
                self.say(passage_text, secret=secret)
            else:
                self.say(json.dumps(payload, ensure_ascii=False, indent=2), secret=secret)
        self.say("  --- END MODEL INPUT ---\n")

    def model_output(self, task, call_id, raw, secret):
        """Show returned answer text before validation; omit hidden reasoning/signatures.

        The existing 128,000-character diagnostic cap is labelled if reached. Printing a
        response does not mean its schema, evidence or IDs passed.

        Args:
            self: Console renderer holding display preferences and the current run identifier.
            task: Workflow task name used to identify the model call in receipts and displays.
            call_id: Application call identifier selecting one saved model attempt, not a
                filesystem path.
            raw: Raw provider reply whose visible text blocks may be displayed; hidden
                reasoning/signatures are omitted.
            secret: Optional exact API key to redact in addition to recognizable credential
                patterns; None adds no exact-key replacement.

        Returns:
            None. Display bounded visible answer text only when opted in, before validation has
            established usability.
        """
        if not self.show_prompts:
            return
        trace = {"non_text_blocks_omitted": False}
        capture_answer(trace, raw)
        self.say(f"\n  --- MODEL REPLY · {task} · {call_id} (not yet validated) ---")
        answer = trace["response_text"]
        try:
            answer = json.dumps(json.loads(answer), ensure_ascii=False, indent=2)
        except ValueError:
            pass
        self.say(answer or "[No answer text returned.]", secret=secret)
        if trace.get("response_text_truncated"):
            self.say("  [Display capped at 128,000 characters; not a complete answer capture.]")
        if trace.get("non_text_blocks_omitted"):
            self.say("  [Non-answer blocks omitted.]")
        self.say("  --- END MODEL REPLY ---\n")

    def node_result(self, node):
        """Summarize useful saved results; never treat a node visit as evidence of success.

        Args:
            self: Console renderer holding display preferences and the current run identifier.
            node: Workflow-node name whose saved results should now be summarized.

        Returns:
            None. Read the latest session projection and print relevant results for this
            completed node.
        """
        session = self.session()
        if node == "plan_research" and session.get("plan"):
            plan = session["plan"]
            self.say(f"  Plan: {short_text(plan['objective'])}")
            for goal in plan["goals"]:
                self.say(f"  • {goal['goal_id']}: {short_text(goal['question'])}")
            for line in scope_lines(plan.get('scope_estimates', [])):
                self.say('  ' + line)
        elif node == "review_plan" and session.get("status") == "plan_ready":
            self.say("  Plan can proceed." if session.get("review_decision") else "  No blocking questions; continuing without a human approval step.")
        elif node == "adjudicate_claims":
            candidates = (session.get("extraction") or {}).get("candidates", [])
            for candidate in candidates[:5]:
                self.say(f"  {candidate['candidate_id']} — {candidate['adjudication_status'].replace('_', ' ')}: "
                    + short_text(candidate["proposal"]["statement"]))
            if len(candidates) > 5:
                self.say(f"  {len(candidates) - 5} more candidates in candidates.md.")
        elif node == "select_followup_sources":
            discovery = session.get("discovery", {})
            context = discovery.get('context_inventory')
            if context:
                self.say(f"  Frontier: {context['total']} eligible; {context['offered']} offered; "
                    f"{context['deferred']} deferred and still pending.")
            parents = {}
            for connection in discovery.get('connections', []):
                if connection['kind'] == 'observed_link':
                    parents.setdefault(connection['target_source_id'], set()).add(connection['parent_source_id'])
            for target, incoming in parents.items():
                signature = (target, tuple(sorted(incoming)))
                if len(incoming) > 1 and signature not in self.reported_intersections:
                    self.say(f"  Shared destination {target}: reached from {', '.join(sorted(incoming))}; reusing the registered source.")
                    self.reported_intersections.add(signature)
            self.say("  " + explain_reason(discovery.get("stop_reason")))
            if discovery.get("stop_reason") == "followup_registered":
                sources = {s["source_id"]: s for s in session.get("sources", [])}
                for choice in discovery.get("selected_sources", []):
                    if choice['source_id'] not in discovery.get('registered_this_round', [c['source_id'] for c in discovery.get('selected_sources', [])]):
                        continue
                    self.say(f"  Next: {source_label(sources[choice['source_id']])}")
                    self.say("    Why: " + short_text(choice["reason"]))
        elif node == 'assess_research_gaps':
            coverage = session.get('coverage', {})
            for line in scope_lines(coverage.get('scope_estimates', [])):
                self.say('  ' + line)
            revision = (coverage.get('scope_estimate_history') or [{}])[-1]
            if revision.get('status') in {'not_revised', 'invalid_revision'}:
                self.say('  Scope estimate retained: ' + revision.get('reason', 'revision unavailable'))
            entry = (coverage.get('history') or [{}])[-1]
            context = entry.get('context_inventory')
            if context:
                self.say(f"  Evidence context: {context['offered']}/{context['total']} intact claims offered; "
                    f"{context['deferred']} deferred. Counts describe the offered evidence.")
            if entry.get('error_code'):
                self.say('  Coverage assessment failed validation: ' + str(entry['error_code']) + '. Research is incomplete.')
            for criterion in coverage.get('criteria_progress', []):
                self.say(f"  {criterion['criterion_id']}: {criterion['status']} — {criterion['observed_count']} model-grouped items (this assessment, not cumulative discovery).")
                if criterion.get('cross_goal_candidate_ids'):
                    self.say('    Cross-goal evidence needs relevance confirmation: ' + ', '.join(criterion['cross_goal_candidate_ids']))

    def knowledge_added(self, event):
        """Show actual new database records so research progress includes the growing graph.

        Args:
            self: Console renderer reading the local store through its output root.
            event: Post-commit event containing run/source IDs and an exclusive/inclusive
                cursor range, not private names, facts or source quotations.

        Returns:
            None. Print every new node, directed edge (including role) and attribute from
            this batch, or explicitly report no new records. Existing endpoints are resolved
            from the store. All text uses the normal sanitized, redacted display path;
            unavailable details are reported without failing already committed research.
        """
        details, source_id = event["details"], event.get("source_id")
        try:
            additions = read_committed_additions(self.output_root / "knowledge.sqlite3", event["run_id"],
                details["knowledge_cursor_start"], details["knowledge_cursor_end"])
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            self.say(f"  {source_id}: knowledge saved; new-record details unavailable. Inspect the knowledge store.")
            return
        counts = {kind: len(rows) for kind, rows in additions.items()}
        if not any(counts.values()):
            self.say(f"  {source_id}: capture batch saved; no new nodes, edges or attributes (records already known or none captured).")
            return
        summary = ", ".join(f"{count} {label}{'s' if count != 1 else ''}"
            for label, count in (("node", counts["nodes"]), ("edge", counts["relationships"]),
                ("attribute", counts["attributes"])))
        self.say(f"  {source_id}: knowledge added — {summary} (source-anchored, unreviewed).")
        for node in additions["nodes"]:
            self.say(f"    + Node [{short_text(node['kind'])}]: {short_text(node['name'])}")
        for edge in additions["relationships"]:
            role = f" [role: {short_text(edge['role'])}]" if edge.get("role") else ""
            self.say(f"    + Edge: {short_text(edge['subject_name'])} — {short_text(edge['predicate'])} → "
                f"{short_text(edge['object_name'])}{role}")
        for attribute in additions["attributes"]:
            self.say(f"    + Attribute: {short_text(attribute['subject_name'])} — {short_text(attribute['predicate'])} = "
                f"{short_text(json.dumps(attribute['value'], ensure_ascii=False))}")

    def __call__(self, event):
        """Translate the selected meaningful events; detailed protocol events stay in JSON logs.

        Args:
            self: Console renderer holding display preferences and the current run identifier.
            event: Whitelisted progress-event dictionary emitted by RunMonitor.

        Returns:
            None. Render selected events, branch-progress decisions and budget summaries at work boundaries. Track
            the retrieval clock across rounds without changing limits; protocol details
            stay in the JSON event log and quiet/JSON modes do not use this sink.
        """
        self.directory = self.output_root / event["run_id"]
        name, details = event["event"], event["details"]
        source_id = event.get("source_id")
        if name == "run_started":
            self.retrieval_clock = None
        if name == "retrieval_started" and self.retrieval_clock is None:
            elapsed = self.session().get("retrieval_usage", {}).get("elapsed_seconds", 0)
            self.retrieval_clock = (elapsed, time.monotonic())
        if "elapsed_seconds" in details:
            # These events carry the actual retrieval counter, not per-call duration.
            self.retrieval_clock = (details["elapsed_seconds"], time.monotonic())
        if name == "run_started":
            self.say(f"Run: {event['run_id']}\nRun folder: {self.directory.resolve()}")
            self.heading("initialize_research")
        elif name == "node_started":
            self.heading(details["node_name"])
        elif name == "node_finished":
            self.node_result(details["node_name"])
        elif name in {'search_query_started', 'search_query_finished'}:
            query = next((q for q in self.session().get('search', {}).get('queries', [])
                if q['query_id'] == details['query_id']), {})
            provider = details['search_provider']
            if name == 'search_query_started':
                self.say(f"  Search {details['query_id']} · {provider}: {short_text(query.get('query', '[query unavailable]'), 300)}")
                self.say('    Why: ' + short_text(query.get('reason', 'No reason recorded.')))
            elif details.get('call_status') == 'completed':
                self.say(f"  Search {details['query_id']} · {provider}: {details['result_count']} results returned (unverified leads).")
            elif details.get('call_status') == 'deferred':
                self.say(f"  Search {details['query_id']} deferred: engines are cooling down; queued page work can continue.")
            else:
                self.say(f"  Search {details['query_id']} · {provider} failed: {details.get('error_code')}")
                diagnostic = query.get('diagnostics', {})
                if diagnostic:
                    self.say('    ' + short_text(f"{diagnostic.get('type', '')}: {diagnostic.get('message', '')}", 600))
        elif name == 'search_engine_started':
            self.say(f"    Trying engine: {details['search_provider']}")
        elif name == 'search_engine_finished' and details.get('error_code'):
            state = 'attempt limit reached' if details.get('engine_exhausted') else 'cooldown scheduled'
            self.say(f"    {details['search_provider']}: {details['error_code']} · {state}; checking other enabled engines.")
        elif name == "source_started":
            source = next((s for s in self.session().get("sources", []) if s["source_id"] == source_id), None)
            action = "Downloading" if source and source["kind"] == "url" else "Reading"
            self.say(f"  {action} {source_id} ({event['source_index']}/{event['source_total']}): "
                + (source_label(source) if source else "registered source"))
        elif name == "source_parsing":
            self.say(f"  {source_id}: converting the document into readable text…")
        elif name == 'exploration_phase_changed':
            labels = {'explore': 'exploring links', 'analyze': 'analyzing available pages',
                'existing_search_results': 'using existing search results', 'assess_gaps': 'reassessing evidence gaps',
                'search': 'ready for a search batch', 'waiting_or_exhausted': 'no ready exploration work'}
            self.say(f"  Exploration cycle {details['exploration_cycle']}: "
                f"{labels.get(details['exploration_phase'], details['exploration_phase'])} "
                f"[up to {details['exploration_depth']} link levels before search].")
        elif name == 'branch_progress_changed':
            status = details['branch_status']
            count = details['unproductive_count']
            self.say(f"  Branch {source_id}: " +
                (explain_reason(status) if status.startswith('branch_') else
                 'new research progress; dry counter reset.' if status == 'useful' else
                 'analyzed with no qualifying new progress.') +
                (f" [{count}/{details['branch_limit']} consecutive unproductive pages]" if count is not None else ''))
        elif name == "source_finished":
            if details.get("source_status") == "retrieved":
                self.say(f"  Saved {source_id}: {details.get('character_count', 0):,} characters → {details.get('text_path')}")
            else:
                self.say(f"  Could not finish {source_id}: {explain_reason(details.get('error_code'))}")
        elif name in {"extraction_call_started", "model_call_started"}:
            label = details.get("batch_id", "model request")
            self.say(f"  Asking the model: {label} · request {details.get('llm_requests', '?')}")
        elif name in {"extraction_call_finished", "model_call_finished"}:
            code = details.get("error_code") or details.get("error_type")
            if details.get("call_status") == "completed":
                result = (f"{details['response_entities']} entities, {details['response_claims']} candidate claims"
                    if name == "extraction_call_finished" else "structured reply received; reference/policy checks follow")
                self.say(f"  {details.get('batch_id')}: {result}. Tokens: {details.get('input_tokens')} in / {details.get('output_tokens')} out.")
            else:
                self.say(f"  {details.get('batch_id')}: {details.get('call_status', 'failed').replace('_', ' ')} — {explain_reason(code)} [{code}]")
            if name == "extraction_call_finished" and details.get("call_status") == "completed":
                attempts = (self.session().get("extraction") or {}).get("attempts", [])
                attempt = next((a for a in attempts if a["batch_id"] == details["batch_id"]), {})
                claims = (attempt.get("result") or {}).get("claims", [])
                for claim in claims[:2]:
                    self.say("    Candidate: " + short_text(claim["statement"]))
                if not claims:
                    self.say("    No claims proposed from this passage; that is not proof of absence.")
        elif name == "knowledge_committed":
            self.knowledge_added(event)
        elif name == "evidence_validation_finished":
            self.say(f"  Checked {details['candidate_count']} candidates; {details['invalid_candidates']} have invalid evidence references.")
        elif name == "claims_adjudicated":
            self.say(f"  {details['supported_claims']} supported by review; {details['unresolved_claims']} unresolved. Nothing published.")
        elif name == "review_required":
            self.say(f"  Waiting for your input: {details.get('question_count', 0)} planning question(s). The run is paused.")
        elif name == "rate_limit_wait":
            self.say(f"  Respecting site pacing; waiting {details.get('wait_seconds')} seconds…")
        elif name == "retrieval_limit_reached":
            self.say("  Retrieval stopped: " + explain_reason(details.get("error_code")))
        elif name == "cancellation_observed":
            self.say("  Cancellation received; saving completed work and stopping.")
        elif name in {"run_finished", "run_failed"}:
            self.say("\nRun stopped: " + str(details.get("run_status", "unknown")).replace("_", " ") + ".")
        if name in {"run_started", "node_finished", "source_finished", "model_call_finished",
                "extraction_call_finished", "search_query_finished", "run_finished", "run_failed"}:
            self.budget_status()
