"""Record document features and observed capture yield for later source prioritization.

Features such as text length and link structure describe what a source might offer;
capture yield records what the optional knowledge task actually extracted. These
helpers save those signals so scheduling or inspection can use explicit data.
They do not reorder or discard documents themselves. A long, richly linked page
is not necessarily authoritative or useful, and a short page may supply the one
fact needed for an answer.
"""
from projectclarity.extraction.saved_source_passages import read_verified_text, EvidenceInputError
from projectclarity.session_storage import save_session, write_text
import json


def source_potential(text, document):
    """Describe text and link structure so later scheduling has explicit signals to use.

    Args:
        text: Hash-verified normalized document text, or None when it cannot be read.
        document: Retrieval receipt containing byte counts, status and observed links.

    Returns:
        A dictionary with potential band, measured signals and heuristic reasons. Unknown
        or thin content is not declared false, irrelevant or unworthy of investigation.
    """
    if text is None:
        return {"potential": "unknown", "reasons": ["source_text_unavailable"], "signals": {}}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    short = sum(2 <= len(line.split()) <= 12 and len(line) <= 120 for line in lines)
    density = round(short / len(lines), 3) if lines else 0
    linked = len(document.links)
    roster_like = len(lines) >= 30 and density >= 0.65
    reasons = []
    if roster_like:
        reasons.append("dense_short_line_structure_possible_directory")
    if linked >= 20:
        reasons.append("many_observed_links")
    if len(text) >= 4000:
        reasons.append("substantial_retrieved_text")
    if len(text) < 300:
        reasons.append("thin_text_or_possible_access_shell")
    band = "high" if roster_like or (linked >= 20 and len(text) >= 4000) else "medium" if len(text) >= 300 else "low"
    return {"potential": band, "reasons": reasons,
        "signals": {"characters": len(text), "nonempty_lines": len(lines), "short_line_fraction": density,
            "observed_links": linked, "link_inventory_truncated": document.links_truncated},
        "interpretation": "Heuristic information potential, not authority, answer relevance or truth. No spending order changed."}


def record_source_values(session, output_root):
    """Save source potential alongside observed capture yield to support later prioritization.

    Args:
        session: ResearchSession whose retrieved documents and existing yield records update.
        output_root: Local directory containing this run's subdirectory.

    Returns:
        None. Update source_value in the session and write source_value.json and session.json.
        A missing or altered source is recorded as unavailable rather than ranked as readable.
    """
    for document in session.documents:
        try:
            text = read_verified_text(output_root / session.session_id, document) if document.status == "retrieved" else None
        except EvidenceInputError:
            text = None
        prior = session.source_value.get(document.source_id, {})
        session.source_value[document.source_id] = {**source_potential(text, document),
            "source_id": document.source_id, "text_sha256": document.text_sha256,
            "observed_yield": prior.get("observed_yield", {}),
            "capture_model_requests": prior.get("capture_model_requests", 0),
            "capture_tokens": prior.get("capture_tokens", {"input_tokens": 0, "output_tokens": 0}),
            "yield_basis": prior.get("yield_basis", "capture_not_started")}
    write_text(output_root / session.session_id / "source_value.json", json.dumps(session.source_value, indent=2) + "\n")
    save_session(session, output_root)
