"""Persist and display early scope hypotheses separately from verified completion targets.

Planning supplies the initial estimate without a new model call. Coverage may revise
it using intact candidates; the original plan never changes. References are checked,
but the model's estimate and confidence are not calibrated statistical measurements.
"""


def initialize_scope(coverage, plan):
    """Seed current scope and history once so later assessments cannot erase the prior.

    Args:
        coverage: Mutable per-run coverage dictionary saved by the calling node.
        plan: Validated ResearchPlan; legacy plans may have no scope estimates.

    Returns:
        None. Initialize missing scope state from the original plan without model calls.
    """
    estimates = [e.model_dump(mode="json") for e in plan.scope_estimates]
    coverage.setdefault("scope_estimates", estimates)
    coverage.setdefault("scope_estimate_history", [{"round": 0, "status": "initial_prior",
        "estimates": estimates}] if estimates else [])


def revise_scope(coverage, answer, session, offered, round_id):
    """Validate revision references and append history, retaining the last valid estimate on failure.

    Args:
        coverage: Mutable per-run coverage state containing initial/current estimates.
        answer: Parsed CoverageAssessment carrying optional complete scope revisions.
        session: ResearchSession with the original plan's goals and estimate identities.
        offered: Still-intact candidate objects actually offered in this assessment.
        round_id: Coverage assessment number attached to this revision or failure.

    Returns:
        None. Append a revision/unchanged/error record and update current estimates only
        for valid proposals. Evidence-informed means referenced, not proven or calibrated.
    """
    current = {e["estimate_id"]: e for e in coverage.get("scope_estimates", [])}
    proposals = [e.model_dump(mode="json") for e in answer.scope_estimates]
    entry = {"round": round_id, "status": "not_revised", "estimates": proposals}
    coverage["scope_estimate_history"].append(entry)
    if not proposals:
        entry["reason"] = "No scope revision returned; previous estimate retained."
        return
    ids = [e["estimate_id"] for e in proposals]
    if len(ids) != len(set(ids)) or set(ids) != set(current):
        entry.update(status="invalid_revision", reason="Scope estimate identities changed or were omitted.")
        return
    for estimate in proposals:
        previous = current[estimate["estimate_id"]]
        if estimate["unit"] != previous["unit"] or set(estimate["goal_ids"]) != set(previous["goal_ids"]):
            entry.update(status="invalid_revision", reason="Scope unit or goals changed; ranges are not comparable.")
            return
        references = estimate["candidate_ids"]
        if set(references) - offered.keys() or any(offered[c].adjudication_status in
                {"invalid_evidence", "contradicted", "out_of_scope"} for c in references):
            entry.update(status="invalid_revision", reason="Unknown, changed or unsuitable scope evidence.")
            return
    entry["status"] = "updated" if proposals != list(current.values()) else "unchanged"
    coverage["scope_estimates"] = proposals


def scope_lines(estimates):
    """Format scope ranges with units, basis and method, never as completion percentages.

    Args:
        estimates: Serialized current estimates or original planner estimates.

    Returns:
        Readable lines for terminal/Markdown callers; callers sanitize untrusted text.
        Empty estimates return an empty list for legacy plans.
    """
    lines = []
    for e in estimates:
        amount = "unknown range" if e["lower"] is None else f"{e['lower']:,}–{e['upper']:,}"
        lines.append(f"Scope {e['estimate_id']}: {amount} {e['unit']} · {e['scale']} · "
            f"{e['basis'].replace('_', ' ')} · {e['confidence']} model confidence (uncalibrated).")
        lines.append("  Estimate method: " + e["method"])
        lines.append("  Estimate rationale: " + e["rationale"])
    return lines
