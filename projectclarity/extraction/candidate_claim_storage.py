"""Save extraction work and render candidate claims for a person's inspection.

After each attempt, extraction.json and session.json preserve its receipt. The
persist_candidates node then writes candidates.json and candidates.md: what was
proposed, which research goal it addresses, and where its quotations occur.
For example, a missing quotation remains visible with quote_not_found, while a
matching quotation is labelled anchored. Optional review/adjudication refreshes
these same files; every candidate still remains not_published.

This is local candidate storage, not the future accepted knowledge database.
Writing a Markdown file is not publication approval or a research report.
Each file is replaced atomically using the existing storage helper, but these
files are not one database transaction. Re-rendering replaces the same IDs; it
does not append duplicates. Entity merging across passages/sources is deferred.
"""

import html
import json
from pathlib import Path

from projectclarity.extraction.extraction_contracts import ExtractionRecord
from projectclarity.plan_renderer import render_plan, terminal_text
from projectclarity.session_storage import save_session, write_text


def save_extraction_record(session, record: ExtractionRecord, output_root: Path) -> None:
    """Keep a typed private record, never raw SDK replies, headers or exceptions."""
    session.extraction = record.model_dump(mode="json")
    write_text(output_root / session.session_id / "extraction.json", record.model_dump_json(indent=2) + "\n")
    save_session(session, output_root)


def display_text(value: str) -> str:
    """Escape HTML/Markdown syntax so candidate prose is displayed as text.

    This is presentation escaping, not a sandbox; a viewer may still autolink
    plain URLs. Never execute or trust instructions merely because they appear
    in this readable candidate file.
    """
    value = html.escape(terminal_text(value), quote=False)
    for character in ("\\", "`", "*", "_", "{", "}", "[", "]", "(", ")", "#", "+", "-", "!", "|"):
        value = value.replace(character, "\\" + character)
    return value


def render_candidates(session, record: ExtractionRecord) -> str:
    """Explain candidates, evidence checks and unfinished coverage in ordinary language."""
    complete = sum(attempt.status == "completed" for attempt in record.attempts)
    lines = ["# ProjectClarity — candidate knowledge", "",
        "NOT ACCEPTED FACTS. Review statuses describe source support; no claims are published.", "",
        f"Run: {session.session_id} | Status: {session.status}",
        f"Passages completed: {complete}/{len(record.attempts)}. Empty findings can be a completed result.",
        f"Source preparation errors: {len(record.source_errors)}. See extraction.json for all receipts.",
        "Offsets are Python character positions in saved normalized text; end positions are exclusive."]
    if record.retrieval_incomplete:
        lines.append("Retrieval was incomplete: not all supplied sources produced text.")
    goals = {goal.goal_id: goal.question for goal in session.plan.goals}
    attempts = {attempt.batch_id: attempt for attempt in record.attempts}
    for candidate in record.candidates:
        claim = candidate.proposal
        lines.extend(["", f"## {candidate.candidate_id}", "", display_text(claim.statement), "",
            f"Evidence check: {candidate.evidence_status}; review: {candidate.review_status}; adjudication: {candidate.adjudication_status}; publication: not published.",
            f"Source: {candidate.source_id}; text SHA-256: {candidate.text_sha256}"])
        if candidate.adjudication_reason:
            lines.append("- Adjudication reason: " + display_text(candidate.adjudication_reason))
        for goal_id in claim.goal_ids:
            lines.append(f"- Goal {goal_id}: {display_text(goals.get(goal_id, 'UNKNOWN GOAL'))}")
        for qualification in claim.qualifications:
            lines.append(f"- Qualification: {display_text(qualification)}")
        batch = attempts[candidate.batch_id].result
        entities = {entity.entity_id: entity.name for entity in batch.entities}
        for entity_id in claim.entity_ids:
            lines.append(f"- Entity {entity_id}: {display_text(entities.get(entity_id, 'UNKNOWN ENTITY'))}")
        if claim.relationship:
            relation = claim.relationship
            lines.append(f"- Relationship: {relation.subject_id} → {display_text(relation.predicate)} → {relation.object_id}")
        if candidate.validation_errors:
            lines.append("- Validation problems: " + ", ".join(candidate.validation_errors))
        for evidence in claim.evidence:
            lines.extend(["", "Proposed supporting passage:"])
            lines.extend("> " + display_text(line) for line in evidence.quote.splitlines())
        for evidence in candidate.evidence:
            lines.append(f"- Located at {evidence.passage_id}, characters {evidence.start}:{evidence.end}.")
    if not record.candidates:
        lines.extend(["", "No candidate claims were saved. This is not proof that no relevant facts exist."])
    return "\n".join(lines) + "\n"


def persist_candidates(state: dict, output_root: Path, monitor=None, *, record_visit=True) -> dict:
    """Save the inspection output and a truthful complete/partial/failed run status."""
    session = state["session"]
    if record_visit:
        session.visited_nodes.append("persist_candidates")
    record = ExtractionRecord.model_validate(session.extraction)
    complete = sum(attempt.status == "completed" for attempt in record.attempts)
    incomplete = (complete != len(record.attempts) or record.source_errors or record.retrieval_incomplete
                  or any(candidate.evidence_status == "invalid" for candidate in record.candidates))
    if session.request.review_claims and "adjudicate_claims" in session.visited_nodes:
        incomplete = incomplete or any(c.adjudication_status == "needs_review" for c in record.candidates)
    if session.status != "cancelled":
        session.status = "extraction_failed" if not complete else "extraction_partial" if incomplete else "candidates_saved"
        if session.status != "candidates_saved":
            session.error = "Extraction, evidence checks or requested claim reviews were incomplete. Inspect extraction.json and candidates.md; no claims were published."
    directory = output_root / session.session_id
    write_text(directory / "candidates.json", json.dumps({
        "schema_version": "1.0", "run_id": session.session_id,
        "candidates": [candidate.model_dump(mode="json") for candidate in record.candidates],
    }, indent=2) + "\n")
    write_text(directory / "candidates.md", render_candidates(session, record))
    save_extraction_record(session, record, output_root)
    write_text(directory / "plan.md", render_plan(session))
    if monitor:
        monitor.observe("candidates_saved", stage=session.status, run_status=session.status,
            candidate_count=len(record.candidates),
            invalid_candidates=sum(c.evidence_status == "invalid" for c in record.candidates))
    return {"session": session}
