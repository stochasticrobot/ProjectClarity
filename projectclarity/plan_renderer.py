"""Render a validated research plan for human review.

Purpose:
    Turn the model's accepted plan into something you can read in the terminal
    or a Markdown file. For example, a goal ID such as G1 appears beside its
    question, the evidence needed, and the proposed steps that address it.
    Both displays use the same text so reviewing plan.md shows the same proposal
    as the command output. Formatting does not approve or execute the plan.
Responsibilities:
    Format the question category, objectives, scope, goals, strategy, assumptions, clarification needs,
    supplied-source IDs, acquisition/candidate status, and usage into Markdown. Filter terminal control
    characters when the CLI prints model- or user-provided text.
Typical use:
    agentic_workflow.py saves render_plan(session) as plan.md; command_line.py displays that same
    representation after passing it through terminal_text(). The canonical
    structured data remains in the session/plan JSON, not in the rendered text.
Boundary:
    These functions return strings; they do not save files, call the model,
    approve plans, or evaluate factual/evidence quality.
"""

from projectclarity.research_schemas import ResearchSession


def render_plan(session: ResearchSession) -> str:
    """Show the proposed plan alongside actual retrieval/extraction and review status.

    This remains a plan display, not a findings report. Explicitly label what
    ran so a completed download or anchored candidate is not mistaken for an
    independently reviewed answer. The caller chooses whether to save or print it.
    """
    plan = session.plan
    if plan is None:
        raise ValueError("Cannot render a session without a validated plan")
    retrieved = "retrieve_sources" in session.visited_nodes
    extracted = session.extraction is not None
    lines = [
        "# ProjectClarity — research plan",
        "",
        f"Session: {session.session_id}",
        f"Status: {session.status.replace('_', ' ')}",
        ("Candidate extraction and claim review attempted. No searches, accepted publication or research report generated."
         if "review_claims" in session.visited_nodes else
         "Candidate extraction attempted. No searches, semantic claim review, accepted publication or research report generated.") if extracted else
        "Source retrieval attempted. No searches, knowledge extraction, or research report generated." if retrieved else "Planning only. No sources fetched or searches executed.",
        "Source text is unverified material, not established facts." if retrieved else "Local document contents have not been read.",
        "",
        "## Original question", session.request.question,
        "", "## Question category", plan.question_category,
        "", "## Objective", plan.objective,
    ]

    def section(title, items):
        """Append a titled list, showing an empty section rather than silently omitting it."""
        lines.extend(["", f"## {title}"])
        lines.extend(f"- {item}" for item in items)
        if not items:
            lines.append("- None listed.")

    section("In scope", plan.scope_in)
    section("Out of scope", plan.scope_out)
    lines.extend(["", "## Research goals and evidence"])
    for goal in plan.goals:
        lines.append(f"- **{goal.goal_id}:** {goal.question}")
        lines.extend(f"  - Evidence needed: {item}" for item in goal.evidence_required)
    lines.extend(["", "## Proposed strategy"])
    for number, step in enumerate(plan.strategy, start=1):
        lines.append(f"{number}. {step.action}")
        lines.append(f"   Goals: {', '.join(step.goal_ids)}")
        lines.append(f"   Supplied sources: {', '.join(step.source_ids) or 'None assigned'}")
        lines.extend(f"   - Proposed search (not run): {query}" for query in step.proposed_searches)
    section("Assumptions to review", plan.assumptions)
    section("Original clarification questions (see human decision below)" if session.review_decision else
            "Clarifications to resolve", plan.clarifications)
    if session.pending_review:
        lines.extend(["", "## Pending plan review", f"Review ID: {session.pending_review['review_id']}",
                      "The run is waiting for your decision; no sources have been retrieved."])
    if session.review_decision:
        decision = session.review_decision
        lines.extend(["", "## Human review decision", f"Action: {decision['action']}",
                      f"Recorded: {session.review_resolved_at}"])
        if decision["action"] == "accept_assumptions":
            lines.append("The reviewer accepted the listed assumptions and explicitly waived unanswered questions; they were not answered by the model.")
        for answer in decision["answers"]:
            lines.append(f"- {answer['question_id']}: {answer['answer']}")
        if decision["answers"]:
            lines.append("These answers are human guidance supplementing the plan, not LLM-generated revisions.")
        if decision.get("note"):
            lines.append(f"Reviewer note: {decision['note']}")
    section("Registered sources (supplied and discovered)" if session.discovery.get("selected_sources") else
            "Supplied sources" if retrieved else "Supplied sources (contents not read)", [
        f"{source.source_id} [{source.kind}]: {source.url if source.kind == 'url' else source.path}"
        for source in session.sources
    ])
    if retrieved:
        successful = sum(document.status == "retrieved" for document in session.documents)
        lines.extend(["", "## Retrieval results", f"{successful}/{len(session.sources)} sources produced readable text."])
        for document in session.documents:
            if document.status == "retrieved":
                lines.append(f"- {document.source_id}: {document.character_count} characters → {document.text_path}")
            else:
                lines.append(f"- {document.source_id}: {document.status} [{document.error_code}] — {document.error}")
    if extracted:
        attempts = session.extraction.get("attempts", [])
        candidates = session.extraction.get("candidates", [])
        lines.extend(["", "## Candidate extraction", f"Passages completed: {sum(a['status'] == 'completed' for a in attempts)}/{len(attempts)}.",
            f"Candidates: {len(candidates)}; invalid evidence checks: {sum(c['evidence_status'] == 'invalid' for c in candidates)}.",
            "Inspect candidates.md for claims and supporting passages. Anchored quotations are not verified facts."])
    usage = session.usage
    lines.extend([
        "", "## Usage and limits",
        f"- LLM requests attempted: {usage.requests_attempted}/{session.request.budget.max_requests}",
        f"- Maximum output tokens per request: {session.request.budget.max_output_tokens}",
        f"- Reported input/output tokens: {usage.input_tokens if usage.input_tokens is not None else 'unknown'} / {usage.output_tokens if usage.output_tokens is not None else 'unknown'}",
        f"- Requested model: {session.request.model}",
        f"- Returned model: {usage.response_model or 'unknown'}",
        "", ("Candidates remain unpublished; consult candidates.md for adjudication and receipts for coverage gaps."
             if "adjudicate_claims" in session.visited_nodes else
             "Candidates remain unreviewed and unpublished; consult extraction receipts for coverage gaps.") if extracted else
        "Acquisition only; no factual claims have been extracted or verified." if retrieved else
        "A human decision is recorded above; no source acquisition has occurred." if session.review_decision else
        "Review this plan before research execution. No approval or execution has been recorded.",
    ])
    return "\n".join(lines) + "\n"


def terminal_text(text: str) -> str:
    """Keep model/source text from emitting terminal escape/control sequences."""
    return "".join(character for character in text if character.isprintable() or character in "\n\t")
