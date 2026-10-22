"""Inspect or request cancellation of a run from a second terminal.

For example, `projectclarity status RUN_ID --watch` shows which document is being
parsed while the research command continues elsewhere. `cancel RUN_ID` writes a
request; it does not claim the worker has already stopped or send a signal to a
possibly reused PID. The worker acknowledges cancellation in its event log.

These commands read small monitoring files, not source bodies or API keys. They
work on the same local filesystem as the worker. File permissions are the access
boundary; this is not a multi-user control API, remote supervisor, or resume tool.
"""

import fcntl
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from projectclarity.research_planner import ResearchError
from projectclarity.run_monitor import safe_text, utc_now


def run_directory(output_root: Path, run_id: str) -> Path:
    """Accept one run name, never a relative path escaping the chosen output folder."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise ResearchError("Invalid run ID. Use the ID printed by the research command.")
    root = output_root.resolve()
    directory = root / run_id
    if directory.is_symlink() or not directory.is_dir():
        raise ResearchError("Run directory not found, or is a symlink. Check --output-dir and the run ID.")
    return directory


def read_status(directory: Path) -> dict:
    """Combine the last snapshot with live lock/control observations, without changing it."""
    path = directory / "run_status.json"
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if snapshot.get("schema_version") != "1.0" or not isinstance(snapshot.get("sources"), list):
            raise ValueError
        with (directory / "worker.lock").open("r") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                running = False
                fcntl.flock(lock, fcntl.LOCK_UN)
            except BlockingIOError:
                running = True
        # A worker may have finished between reading its status and its lock.
        if not running and not snapshot["finished"]:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        snapshot["worker_running"] = running
        snapshot["interrupted"] = not running and not snapshot["finished"] and not snapshot.get("waiting_for_input", False)
        snapshot["cancel_requested"] = (directory / "cancel.request").exists() or snapshot.get("cancel_requested", False)
        updated = datetime.fromisoformat(snapshot["updated_at"])
        snapshot["activity_age_seconds"] = round(max(0, (datetime.now(UTC) - updated).total_seconds()), 1)
        return snapshot
    except FileNotFoundError:
        raise ResearchError("Monitoring files are not available for this run. Older runs were not monitored; a new run may still be initializing.") from None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ResearchError("Cannot read the run's monitoring files. Check their format and permissions.") from None


def render_status(snapshot: dict) -> str:
    """Show observed counts, not guessed completion percentages or download ETAs."""
    counts = snapshot["counts"]
    metrics = snapshot["metrics"]
    lines = [f"Run: {snapshot['run_id']} | status: {snapshot['status']} | stage: {snapshot['stage']}",
             f"Worker: {'running' if snapshot['worker_running'] else 'stopped'} | last activity: {snapshot['activity_age_seconds']}s ago",
             f"Sources: {counts['completed']}/{counts['total']} completed; {counts['retrieved']} retrieved, "
             f"{counts['failed']} failed, {counts['cancelled']} cancelled, {counts['not_started']} not started",
             f"HTTP requests: {metrics.get('http_requests', 0)} | bytes read (including robots): {metrics.get('bytes_read', 0)}",
             f"Elapsed at last update: {snapshot['elapsed_seconds']}s | active source: {snapshot['active_source_id'] or 'none'}",
             f"Configured fetch limits: {json.dumps(snapshot['limits'], sort_keys=True)}"]
    if snapshot["interrupted"]:
        lines.append("INTERRUPTED: worker lock is released but no final status was recorded. This run cannot resume.")
    if "passages_total" in metrics:
        lines.append(f"Extraction: {metrics.get('passages_completed', 0)}/{metrics['passages_total']} passages completed; "
                     f"{metrics.get('candidate_count', 0)} candidates, {metrics.get('invalid_candidates', 0)} invalid evidence checks; "
                     f"{metrics.get('llm_requests', 0)} total LLM attempts")
        if snapshot.get("llm_limits"):
            lines.append(f"Configured LLM limits: {json.dumps(snapshot['llm_limits'], sort_keys=True)}")
    if snapshot.get("waiting_for_input"):
        lines.append("WAITING FOR PLAN REVIEW: no worker is needed while you decide. Use review RUN_ID to see the questions.")
    if snapshot["cancel_requested"]:
        lines.append("Cancellation requested; inspect status/events for acknowledgement and final outcome.")
    for source in snapshot["sources"]:
        lines.append(f"  {source['source_id']} [{source['kind']}] {source['status']}/{source['stage']} "
                     f"bytes={source['byte_count']} chars={source['character_count']} "
                     f"seconds={source['elapsed_seconds']} error={source['error_code'] or '-'} "
                     f"text={source['text_path'] or '-'}")
    return "\n".join(safe_text(line) for line in lines)


def show_status(output_root: Path, run_id: str, *, watch=False, json_output=False) -> int:
    """Print once, or watch until finished, interrupted or waiting for human review.

    Watching reports observations; it neither keeps a worker alive nor resumes
    one. A paused review stops the watch because no progress is expected until
    the person submits a decision.
    """
    directory = run_directory(output_root, run_id)
    while True:
        snapshot = read_status(directory)
        print(json.dumps(snapshot, ensure_ascii=True) if json_output else render_status(snapshot), flush=True)
        if not watch or snapshot["finished"] or snapshot["interrupted"] or snapshot.get("waiting_for_input"):
            return 0
        time.sleep(1)


def request_cancel(output_root: Path, run_id: str) -> str:
    """Record an idempotent request; only the worker can record completed cancellation."""
    directory = run_directory(output_root, run_id)
    snapshot = read_status(directory)
    if snapshot["finished"]:
        return f"Run {run_id} is already finished ({snapshot['status']}); no cancellation requested."
    if snapshot["interrupted"]:
        raise ResearchError("The worker is no longer running. A stop request cannot resume or repair this run.")
    if snapshot.get("waiting_for_input") and not snapshot["worker_running"]:
        raise ResearchError("This run is paused for review, not fetching. Use review RUN_ID --review-id ID --reject to end it.")
    try:
        with (directory / "cancel.request").open("x", encoding="utf-8") as stream:
            stream.write(json.dumps({"requested_at": utc_now()}) + "\n")
    except FileExistsError:
        pass
    except OSError:
        raise ResearchError("Cannot write the cancellation request. Check run-directory permissions.") from None
    return f"Cancellation requested for {run_id}. It is not stopped until the worker acknowledges and finishes."
