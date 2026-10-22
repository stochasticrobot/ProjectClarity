"""Tell the story of a running research task in an ordinary terminal.

For example: 'Retrieve sources', 'Downloading S1: https://example.org/team',
then 'Saved 3,200 characters'. This is a presentation layer, not a graph node.
RunMonitor sends safe events here; private source labels are read separately
from the run snapshot. They never get added to events.jsonl or status telemetry.

With --show-prompts, explicit private callbacks also display the actual model
messages and returned answer text. Those callbacks do not write operational logs,
change requests, reconstruct old inputs, or make another model call. Prompts may
contain full document passages. Credentials are redacted and terminal control
characters removed. No Rich/TUI dependency or interactive terminal is required.
"""
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from projectclarity.extraction.extraction_call_tracing import capture_answer, redact_text
from projectclarity.plan_renderer import terminal_text


NODE_EXPLANATIONS = {
    "initialize_research": ("Set up research", "Registering your sources and execution limits; not reading document contents yet."),
    "plan_research": ("Plan the research", "Asking the model to turn your question into research goals and a strategy."),
    "review_plan": ("Check the plan", "Checking whether we need your input before collecting sources."),
    "retrieve_sources": ("Retrieve sources", "Downloading permitted pages or reading supplied documents, then saving text and provenance."),
    "extract_knowledge": ("Extract candidate knowledge", "Reading saved passages with the model to identify relevant people, organizations and claims."),
    "validate_evidence": ("Check evidence references", "Checking source versions, identifiers and exact quotations. This does not establish truth."),
    "persist_candidates": ("Save candidate claims", "Saving claims and supporting passages for inspection; nothing is published."),
    "review_claims": ("Review claim support", "Asking a separate model task whether the original passage supports each claim."),
    "adjudicate_claims": ("Assign claim statuses", "Applying Python rules to review results and rechecking the saved evidence."),
    "select_followup_sources": ("Consider follow-up sources", "Checking useful observed links, permissions and remaining budgets; at most one hop."),
}

REASONS = {
    "single_hop_complete": "The permitted follow-up hop is complete; no second hop will run.",
    "no_eligible_links": "No new links meet this run's source permissions.",
    "no_useful_links_selected": "The selector found no useful follow-up links in the offered list.",
    "request_or_source_limit": "There is not enough remaining request or source allowance for another investigation.",
    "fetch_budget_exhausted": "The shared download time, HTTP-request or byte allowance is exhausted.",
    "followup_registered": "Follow-up sources are registered and will now be retrieved.",
    "supplied_sources_only": "This run is limited to the sources you supplied.",
    "provider_unavailable": "An earlier model request failed; no further selection request will be sent.",
    "invalid_selection_references": "The model selected invalid link/goal IDs or exceeded the selection limit; nothing new was authorized.",
    "request_limit": "The model-request allowance is exhausted.",
    "invalid_extraction_schema": "The extraction reply did not match the required answer format.",
    "invalid_output_schema": "The model reply did not match the required answer format.",
    "incomplete_model_output": "The model did not finish its answer, possibly because of the output-token cap.",
    "review_quote_not_found": "A reviewer quotation could not be matched to the source passage; that review was not accepted.",
    "review_ambiguous_quote": "A reviewer quotation has more than one possible source location; that review was not accepted.",
    "provider_or_adapter_error": "The model request failed; inspect its receipt for the recorded outcome.",
    "cancelled": "Stopped at your cancellation request; earlier work is retained.",
}


def short_text(value, limit=200):
    """Make an untrusted preview one line; explicit prompt mode does not use this shortening."""
    value = " ".join(terminal_text(str(value)).split())
    return value if len(value) <= limit else value[:limit] + "…"


def explain_reason(code):
    """Translate known stop/failure codes, retaining unfamiliar codes for diagnosis."""
    return REASONS.get(code, str(code or "No additional detail was recorded.").replace("_", " "))


def source_label(source):
    """Show a useful URL/file label without URL credentials, fragments or query values."""
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
        """Remember display options only; don't read files or start a research run."""
        self.output_root = Path(output_root)
        self.show_prompts = show_prompts
        self.directory = None
        self.step = 0

    def say(self, text="", *, secret=None):
        """Write private human-facing text safely, without attaching it to operational events."""
        safe, _ = redact_text(str(text), secret)
        print(terminal_text(safe), file=sys.stderr, flush=True)

    def session(self):
        """Read the latest inspectable projection; missing snapshots must not fail research."""
        if self.directory is None:
            return {}
        try:
            return json.loads((self.directory / "session.json").read_text())
        except (OSError, ValueError):
            return {}

    def heading(self, node):
        """Identify the actual executing node and explain its job without jargon."""
        title, explanation = NODE_EXPLANATIONS.get(node, (node.replace("_", " "), ""))
        self.step += 1
        self.say(f"\nStep {self.step} · {title} [{node}]")
        if explanation:
            self.say("  " + explanation)

    def model_input(self, task, call_id, messages, schema, secret):
        """Display the very messages about to be invoked, not a separately rebuilt prompt.

        The schema name identifies the separate structured-output contract; its
        full JSON remains in extraction/review/discovery traces. This view shows
        application messages, not hidden provider instructions or HTTP headers.
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

        The existing 128,000-character diagnostic cap is labelled if reached.
        Printing a response does not mean its schema, evidence or IDs passed.
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
        """Summarize useful saved results; never treat a node visit as evidence of success."""
        session = self.session()
        if node == "plan_research" and session.get("plan"):
            plan = session["plan"]
            self.say(f"  Plan: {short_text(plan['objective'])}")
            for goal in plan["goals"]:
                self.say(f"  • {goal['goal_id']}: {short_text(goal['question'])}")
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
            self.say("  " + explain_reason(discovery.get("stop_reason")))
            if discovery.get("stop_reason") == "followup_registered":
                sources = {s["source_id"]: s for s in session.get("sources", [])}
                for choice in discovery.get("selected_sources", []):
                    self.say(f"  Next: {source_label(sources[choice['source_id']])}")
                    self.say("    Why: " + short_text(choice["reason"]))

    def __call__(self, event):
        """Translate the selected meaningful events; detailed protocol events stay in JSON logs."""
        self.directory = self.output_root / event["run_id"]
        name, details = event["event"], event["details"]
        source_id = event.get("source_id")
        if name == "run_started":
            self.say(f"Run: {event['run_id']}\nRun folder: {self.directory.resolve()}")
            self.heading("initialize_research")
        elif name == "node_started":
            self.heading(details["node_name"])
        elif name == "node_finished":
            self.node_result(details["node_name"])
        elif name == "source_started":
            source = next((s for s in self.session().get("sources", []) if s["source_id"] == source_id), None)
            action = "Downloading" if source and source["kind"] == "url" else "Reading"
            self.say(f"  {action} {source_id} ({event['source_index']}/{event['source_total']}): "
                + (source_label(source) if source else "registered source"))
        elif name == "source_parsing":
            self.say(f"  {source_id}: converting the document into readable text…")
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
