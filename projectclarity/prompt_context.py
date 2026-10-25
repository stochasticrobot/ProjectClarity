"""Supply research guidance with its meaning intact, without workflow metadata."""


def research_context(session):
    """Pair clarification answers with their questions for scoped research tasks.

    Args:
        session: Research session with an active plan and optional saved human review.
            Older reviews may lack question text; recover it from an unchanged plan
            only, never from a replacement plan with different clarifications.

    Returns:
        A fresh payload fragment containing the run's research date, working
        assumptions, question/answer guidance and optional human note. Missing
        historical question text is explicitly null; no files or state are changed.
    """
    decision = session.review_decision or {}
    questions = {} if decision.get("revised_plan") else {
        f"Q{i}": question for i, question in enumerate(session.plan.clarifications, 1)}
    guidance = [{"question": answer.get("question") or questions.get(answer["question_id"]),
                 "answer": answer["answer"]} for answer in decision.get("answers", [])]
    return {"research_date": session.created_at[:10],
            "working_assumptions": list(session.plan.assumptions),
            "human_guidance": guidance, "human_note": decision.get("note")}
