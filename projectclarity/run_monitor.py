"""Record what a research worker is doing while it is still running.

For example, S1 can move from downloading to parsing to saved while S2 is still
pending. The graph and retrieval code report those facts here; this file writes
events.jsonl (one timestamped JSON event per line) and run_status.json (the latest
snapshot), then optionally shows the event in the research terminal.
Extraction also reports passage IDs, result counts, tokens, durations and failures.
Actual prompts/replies live separately in extraction_calls/ and llm_calls/, not
in these events. Optional private display callbacks send current messages to
terminal_run_progress.py without putting them in operational records. The default
human display explains meaningful activity; --progress-json retains raw events.

This is local, single-worker monitoring, not a queue, database, or remote tracing
service. A process-held file lock lets another command distinguish a running
worker from a snapshot abandoned by a crash. A cancel.request file asks the worker
to stop at its next control check. Callers supply content-free event fields;
start() receives a session but selects only IDs, hostnames and limits for storage.
The allowlist checks field names, not arbitrary values: it is not a redactor.
Do not pass source text, prompts, credentials, URL queries or full local paths
as event values. The separate final CLI summary deliberately shows research text.
"""

import fcntl
import json
import os
import signal
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from projectclarity.retrieval.retrieval_limits import RunCancelled


class MonitoringError(RuntimeError):
    """Monitoring could not be recorded; the worker must not continue invisibly."""


def utc_now() -> str:
    """Use timezone-aware timestamps so event files are comparable across runs."""
    return datetime.now(UTC).isoformat()


def safe_text(value) -> str:
    """Keep generated display labels on one line and free of terminal controls."""
    return "".join(char for char in str(value) if char.isprintable())[:240]


def console_progress(event: dict, *, json_output=False) -> None:
    """Write safe live metadata to stderr, leaving stdout for plans/candidate summaries.

    JSON mode is a machine-readable event stream. Human mode selects a smaller
    set of counters/status fields; neither format includes private call bodies.
    """
    import sys
    if json_output:
        line = json.dumps(event, ensure_ascii=True)
    else:
        source = f" [{event['source_index']}/{event['source_total']}] {event['source_id']}" if event.get("source_id") else ""
        details = event["details"]
        values = " ".join(f"{key}={safe_text(value)}" for key, value in details.items()
                          if key in {"stage", "source_status", "run_status", "http_status", "error_code",
                                     "byte_count", "character_count", "text_path", "wait_seconds", "host",
                                     "passages_total", "passages_completed", "candidate_count", "invalid_candidates", "llm_requests",
                                     "batch_id", "call_status", "response_entities", "response_claims", "input_tokens",
                                     "output_tokens", "call_seconds", "stop_reason", "trace_path",
        "supported_claims", "unresolved_claims", "selected_sources", "eligible_links", "error_type", "supported_claims", "unresolved_claims", "selected_sources", "eligible_links"})
        line = f"[{event['timestamp'][11:19]}] {event['event']}{source} {values}".rstrip()
    print(line, file=sys.stderr, flush=True)


class RunMonitor:
    """One owner writes events and snapshots; readers and cancel requests are separate."""

    # An allowlist prevents an accidental `response=...` or `api_key=...` from
    # turning operational logging into a dump of confidential application data.
    DETAIL_FIELDS = {
        "stage", "run_status", "source_status", "error_code", "error_type", "host", "node_name",
        "request_kind", "http_status", "wait_seconds", "request_seconds", "source_seconds",
        "byte_count", "character_count", "text_path", "raw_path", "http_requests", "bytes_read",
        "elapsed_seconds", "max_total_bytes", "max_source_bytes", "max_http_requests",
        "review_id", "review_kind", "question_count", "review_action",
        "passages_total", "passages_completed", "candidate_count", "invalid_candidates", "llm_requests",
        "batch_id", "passage_id", "call_status", "response_entities", "response_claims", "input_tokens",
        "output_tokens", "call_seconds", "stop_reason", "trace_path",
        "supported_claims", "unresolved_claims", "selected_sources", "eligible_links",
    }

    def __init__(self, output_root: Path, sink=None):
        """Prepare an idle monitor; start/attach later claim a run and open its files.

        The optional sink displays events without owning the durable record.
        Constructing this object alone does not start a worker or create a queue.
        """
        self.output_root = output_root
        self.sink = sink
        self.directory = None
        self.snapshot = None
        self.lock = None
        self.cancel_from_signal = False
        self.cancel_acknowledged = False
        self.started = time.monotonic()
        self.last_snapshot = 0.0
        self.sequence = 0
        self.active_source = None
        self.prior_elapsed = 0.0

    def start(self, session) -> None:
        """Publish the run ID and pending sources before planning or document I/O."""
        self.directory = self.output_root / session.session_id
        self.lock = (self.directory / "worker.lock").open("a")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.snapshot = {
            "schema_version": "1.0", "run_id": session.session_id, "pid": os.getpid(),
            "created_at": utc_now(), "updated_at": utc_now(), "status": "initialized",
            "finished": False, "stage": "initializing", "active_source_id": None,
            "waiting_for_input": False,
            "limits": session.request.fetch_limits.model_dump(), "metrics": {}, "sources": [],
            "llm_limits": session.request.budget.model_dump(),
        }
        for source in session.sources:
            # IDs map back to the private source list in session.json. Logging
            # the hostname (not URL path/query) avoids leaking signed URL tokens.
            self.snapshot["sources"].append({
                "source_id": source.source_id, "kind": source.kind,
                "host": urlsplit(source.url).hostname if source.kind == "url" else None,
                "status": "pending", "stage": "pending", "byte_count": 0,
                "character_count": 0, "elapsed_seconds": None, "error_code": None,
                "text_path": None,
            })
        self.observe("run_started", stage="initialized", run_status="initialized")

    def register_sources(self, sources) -> None:
        """Add discovery-approved IDs to live monitoring without replacing earlier outcomes."""
        known = {source["source_id"] for source in self.snapshot["sources"]}
        for source in sources:
            if source.source_id not in known:
                self.snapshot["sources"].append({"source_id": source.source_id, "kind": source.kind,
                    "host": urlsplit(source.url).hostname if source.kind == "url" else None,
                    "status": "pending", "stage": "pending", "byte_count": 0, "character_count": 0,
                    "elapsed_seconds": None, "error_code": None, "text_path": None})
        self.observe("sources_registered", stage="discovery")

    def attach(self, directory: Path) -> None:
        """Exclusively claim an existing run before reading or submitting a review.

        Taking the same lock as the worker prevents two terminals from resuming
        the run at once. This does not yet alter its saved status or event log.
        """
        self.directory = directory
        self.lock = (directory / "worker.lock").open("r+")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.snapshot = json.loads((directory / "run_status.json").read_text(encoding="utf-8"))
        self.sequence = self.snapshot["last_event_sequence"]
        self.prior_elapsed = self.snapshot["elapsed_seconds"]

    def resume(self) -> None:
        """Reopen execution after validation; human waiting time is not worker time."""
        self.started = time.monotonic()
        self.snapshot.update(pid=os.getpid(), finished=False, waiting_for_input=False)
        self.snapshot.pop("finished_at", None)
        self.observe("run_resumed", stage="review_plan", run_status="awaiting_review")

    def pause_for_review(self, review: dict) -> None:
        """Announce a durable interrupt; questions remain in the private review payload."""
        self.snapshot["waiting_for_input"] = True
        self.snapshot["review_id"] = review["review_id"]
        self.observe("review_required", run_status="awaiting_review", stage="review_plan",
                     review_id=review["review_id"], review_kind="plan_review",
                     question_count=len(review["questions"]))

    def _write_snapshot(self) -> None:
        """Replace a complete snapshot; readers never need to parse half-written JSON."""
        now = time.monotonic()
        counts = Counter(source["status"] for source in self.snapshot["sources"])
        self.snapshot["counts"] = {name: counts[name] for name in
            ("pending", "active", "retrieved", "failed", "cancelled", "not_started")}
        self.snapshot["counts"]["total"] = len(self.snapshot["sources"])
        self.snapshot["counts"]["completed"] = sum(counts[name] for name in ("retrieved", "failed", "cancelled"))
        self.snapshot["error_counts"] = dict(Counter(source["error_code"] for source in self.snapshot["sources"]
                                                    if source["error_code"]))
        self.snapshot["elapsed_seconds"] = round(self.prior_elapsed + now - self.started, 3)
        self.snapshot["updated_at"] = utc_now()
        self.snapshot["last_event_sequence"] = self.sequence
        temporary = self.directory / "run_status.json.tmp"
        temporary.write_text(json.dumps(self.snapshot, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        temporary.replace(self.directory / "run_status.json")
        self.last_snapshot = now

    def observe(self, event: str, *, source_id=None, **details) -> None:
        """Accept selected metadata from a node, budget, HTTP request, or parser stage."""
        if self.snapshot is None:
            return
        if set(details) - self.DETAIL_FIELDS:
            raise ValueError("Unsupported monitoring field")
        if event in {"source_started", "extraction_call_started"}:
            self.active_source = source_id
        source_id = source_id or self.active_source
        source = next((item for item in self.snapshot["sources"] if item["source_id"] == source_id), None)
        if "run_status" in details:
            self.snapshot["status"] = details["run_status"]
        if "stage" in details:
            self.snapshot["stage"] = details["stage"]
            if source:
                source["stage"] = details["stage"]
        if source and event == "source_started":
            source["status"] = "active"
            source["started_at"] = utc_now()
        if source:
            for name in ("byte_count", "character_count", "text_path", "error_code"):
                if name in details:
                    source[name] = details[name]
            if "source_seconds" in details:
                source["elapsed_seconds"] = details["source_seconds"]
            if "source_status" in details:
                source["status"] = details["source_status"]
                source["stage"] = details["source_status"]
                source["finished_at"] = utc_now()
        for name in ("http_requests", "bytes_read", "elapsed_seconds", "max_total_bytes",
                     "max_source_bytes", "max_http_requests", "passages_total", "passages_completed",
                     "candidate_count", "invalid_candidates", "llm_requests", "supported_claims",
                     "unresolved_claims", "selected_sources", "eligible_links"):
            if name in details:
                self.snapshot["metrics"][name] = details[name]
        if event == "source_finished":
            self.active_source = None
            self.snapshot["stage"] = "retrieving"
        if event in {"extraction_call_finished", "extraction_finished"}:
            self.active_source = None
        self.snapshot["active_source_id"] = self.active_source
        # Byte counters are sampled to disk, not logged for every 64 KiB chunk.
        # Final stage events force a fresh snapshot, even for very small files.
        if event == "bytes_read":
            if time.monotonic() - self.last_snapshot < 0.5:
                return
            try:
                self._write_snapshot()
            except OSError:
                raise MonitoringError("Cannot update run monitoring files.") from None
            return
        self.sequence += 1
        if details.get("source_status") == "failed" or details.get("call_status") == "failed" or event == "run_failed" or details.get("run_status") in {"failed", "retrieval_failed", "extraction_failed"}:
            level = "error"
        elif details.get("run_status") in {"retrieval_partial", "needs_clarification", "extraction_partial"}:
            level = "warning"
        else:
            level = "info"
        event_record = {"schema_version": "1.0", "sequence": self.sequence, "timestamp": utc_now(),
            "level": level, "run_id": self.snapshot["run_id"], "source_id": source_id,
            "source_index": next((index for index, item in enumerate(self.snapshot["sources"], 1)
                                  if item["source_id"] == source_id), None),
            "source_total": len(self.snapshot["sources"]),
            "event": event, "details": details}
        try:
            with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event_record, ensure_ascii=True) + "\n")
            self._write_snapshot()
        except OSError:
            raise MonitoringError("Cannot write run monitoring files.") from None
        if self.sink:
            try:
                self.sink(event_record)
            except (BrokenPipeError, OSError):
                # A disconnected terminal is not the authoritative event store.
                # Keep the persisted audit trail and stop writing to that sink.
                self.sink = None

    def model_input(self, task, call_id, messages, schema, secret) -> None:
        """Offer private input to an opted-in UI, NEVER to observe()/events/checkpoints.

        A plain event callback or JSON sink has no such method, so receives no
        source content. Credentials are transient redaction inputs, never stored.
        """
        callback = getattr(self.sink, "model_input", None)
        if callback:
            try:
                callback(task, call_id, messages, schema, secret)
            except (BrokenPipeError, OSError):
                self.sink = None

    def model_output(self, task, call_id, raw, secret) -> None:
        """Offer answer text to the private UI without logging the provider object."""
        callback = getattr(self.sink, "model_output", None)
        if callback:
            try:
                callback(task, call_id, raw, secret)
            except (BrokenPipeError, OSError):
                self.sink = None

    def check_cancel(self) -> None:
        """A request is acknowledged only when the worker actually observes it."""
        if self.cancel_from_signal or (self.directory and (self.directory / "cancel.request").exists()):
            if not self.cancel_acknowledged:
                self.cancel_acknowledged = True
                if self.snapshot:
                    self.snapshot["cancel_requested"] = True
                    self.observe("cancellation_observed")
            raise RunCancelled("Run cancellation requested.")

    def finish(self, status: str, *, error_type=None) -> None:
        """Close the run truthfully and distinguish unfinished sources from failures."""
        if self.snapshot is None or self.snapshot["finished"]:
            return
        for source in self.snapshot["sources"]:
            if source["status"] in {"pending", "active"}:
                if source["status"] == "active":
                    source["status"] = "cancelled" if status == "cancelled" else "failed"
                    source["error_code"] = source["error_code"] or ("cancelled" if status == "cancelled" else "worker_failure")
                else:
                    source["status"] = "not_started"
                source["stage"] = source["status"]
        self.active_source = None
        self.snapshot["finished"] = True
        self.snapshot["waiting_for_input"] = False
        if status == "cancelled":
            self.snapshot["cancel_requested"] = True
        self.snapshot["finished_at"] = utc_now()
        self.observe("run_failed" if error_type and status != "cancelled" else "run_finished", run_status=status, stage=status,
                     **({"error_type": error_type} if error_type else {}))

    def close(self) -> None:
        """Release the worker lock without inventing a final status.

        The OS also releases it on process death. Status readers can then detect
        an unfinished abandoned run rather than assuming the stale PID is alive.
        """
        if self.lock:
            self.lock.close()
            self.lock = None

    @contextmanager
    def termination_handler(self):
        """Convert SIGTERM into a cooperative request; keep Ctrl-C's normal exception."""
        previous = None
        if threading.current_thread() is threading.main_thread():
            previous = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, lambda *_: setattr(self, "cancel_from_signal", True))
        try:
            yield
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)
