"""Collect the saved background information included in research-model requests.

ProjectClarity sends a language model both task instructions and information about
the current research run. This module prepares the latter: the research date,
assumptions in the plan, the user's answers to clarification questions, and any
saved specification of the report's sections and required fields. For example,
"Only Canadian universities" stays paired with the question it answered, rather
than being sent as an unexplained response.

If an optional preliminary source study has finished, its estimates, limitations
and proposed evidence checks are included with labels saying they are preliminary.
They are not promoted to verified findings or exact completion targets. Long study
text is shortened for the request. This module reads the supplied session in
memory; it does not read files, contact a model, or save anything. Task instructions
are loaded separately from text files by prompt_loader.py.
"""


def research_context(session):
    """Build the background-data dictionary attached to research-model requests.

    Keep the user's clarification answers paired with their original questions and
    include the plan's assumptions, run date and optional report specification.
    This gives each task enough saved context without issuing a model request here.

    Args:
        session: ResearchSession with a non-null plan, creation date, optional human
            review decision, optional answer schema and optional preliminary study.
            A review made against a replaced plan must carry its original question
            text; a missing question is not guessed from the replacement plan.

    Returns:
        A fresh dictionary with research_date, working_assumptions, human_guidance
        and human_note, plus answer_schema when present. A finished preliminary study
        adds estimates, source IDs, limitations, proposed evidence requirements and
        their latest assessment statuses. Those study values are explicitly labeled
        preliminary and not completion targets. Unit/method/limitation descriptions
        are shortened, at most four limitations are included, and omitted question
        text is None. Nested saved structures may be shared with the session; this
        function does not modify them, read files, or contact a model.
    """
    decision = session.review_decision or {}
    questions = {} if decision.get("revised_plan") else {
        f"Q{i}": question for i, question in enumerate(session.plan.clarifications, 1)}
    guidance = [{"question": answer.get("question") or questions.get(answer["question_id"]),
                 "answer": answer["answer"]} for answer in decision.get("answers", [])]
    context = {"research_date": session.created_at[:10],
            "working_assumptions": list(session.plan.assumptions),
            "human_guidance": guidance, "human_note": decision.get("note")}
    if session.answer_schema:
        context['answer_schema'] = session.answer_schema
    study = session.coverage_study
    if study and study.get('status') != 'running':
        context['early_coverage_study'] = {'status': study['status'],
            'estimates': [{**{k: e[k] for k in ('estimate_id', 'goal_ids', 'range', 'basis')},
                'unit': e['unit'][:160], 'method': e['method'][:400],
                'source_ids': list(dict.fromkeys(r['source_id'] for r in e['evidence']))}
                for e in study.get('estimates', [])],
            'limitations': [text[:200] for text in study.get('limitations', [])[:4]],
            'coverage_boundary': (study.get('coverage_boundary') or '')[:800],
            'context_is_condensed': True,
            'review_status': 'preliminary_not_claim_reviewed', 'completion_target': False}
        if study.get('readiness'):
            context['early_coverage_study'].update(coverage_outcome=study.get('coverage_outcome', 'unknown'),
                readiness=study['readiness'],
                readiness_progress=session.coverage.get('readiness_progress', []))
    return context
