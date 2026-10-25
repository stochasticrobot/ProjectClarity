"""Present saved evidence as a provisional answer without spending model calls.

This is extractive reporting, not entity resolution or new semantic synthesis.
It reuses the production coverage integrity check and never promotes a matching
quotation alone to an answer. Nothing here changes model prompts or schemas.
"""

from pathlib import Path

from projectclarity.nodes.assess_research_gaps.node import checked_candidates
from projectclarity.plan_renderer import terminal_text


def build_provisional_answer(session, output_root: Path) -> dict:
    """Partition intact claims so final reporting survives exhausted model budgets.

    Args:
        session: Typed run snapshot containing the question, goals and review results.
        output_root: Parent of the run directory; saved source text is read only.

    Returns:
        Plain application-owned data with supported answer findings, counterevidence,
        disputed claims and leads. Exact statement/qualification/goal duplicates
        merge citations, not identities. Missing/altered evidence is excluded;
        malformed extraction contracts raise validation errors rather than inventing
        an answer. No session mutation, network requests or file writes occur.
    """
    goals = {g.goal_id: g.question for g in session.plan.goals} if session.plan else {}
    answer = dict(question=session.request.question, run_id=session.session_id,
        status=session.status, stop_reason=session.workflow.get('stop_reason'),
        stop_details=session.workflow.get('stop_details', []), goals=goals,
        coverage=session.coverage.get('assessment_status', 'not_assessed'),
        gaps=[gap for item in session.coverage.get('criteria_progress', [])
              for gap in item.get('gaps', [])],
        findings=[], counterevidence=[], disputed=[], leads=[], integrity_excluded=0)
    if not session.extraction:
        return answer
    record, valid = checked_candidates(session, output_root)
    answer['integrity_excluded'] = sum(c.evidence_status == 'anchored'
        and c.candidate_id not in valid for c in record.candidates)
    documents = {d.source_id: d for d in session.documents}
    merged = {}
    for candidate in valid.values():
        proposal = candidate.proposal
        roles = set(proposal.research_roles)
        goal_ids = sorted(set(proposal.goal_ids) & goals.keys())
        verdict = candidate.adjudication_status
        if verdict == 'supported' and 'counterevidence' in roles:
            section = 'counterevidence'
        elif not goal_ids:
            continue
        elif verdict == 'supported' and 'answer_evidence' in roles:
            section = 'findings'
        elif verdict == 'contradicted' and roles & {'answer_evidence', 'lead'}:
            section = 'disputed'
        elif verdict in {'supported', 'pending', 'needs_review', 'insufficient_evidence'} and roles & {'answer_evidence', 'lead'}:
            section = 'leads'
        else:
            continue
        key = (section, ' '.join(proposal.statement.split()), tuple(proposal.qualifications),
               tuple(goal_ids), verdict)
        if key not in merged:
            row = dict(statement=proposal.statement, qualifications=list(proposal.qualifications),
                goal_ids=goal_ids, review_status=verdict, citations=[])
            merged[key] = row
            answer[section].append(row)
        document = documents[candidate.source_id]
        merged[key]['citations'].append(dict(candidate_id=candidate.candidate_id,
            source_id=candidate.source_id,
            location=str(document.final_url or document.original_location),
            evidence=[e.model_dump(mode='json') for e in candidate.evidence]))
    return answer


def render_provisional_answer(answer: dict, *, include_leads: bool = False) -> str:
    """Render actual answer statements, qualifications and citations, not just counts.

    Args:
        answer: Data returned by build_provisional_answer after source checks.
        include_leads: Include every unresolved lead in the saved/full display;
            default terminal output retains all findings and counterevidence but
            reports the lead count separately to keep hypotheses out of the answer.

    Returns:
        Terminal-safe Markdown with no first-N findings cap. It states uncertainty,
        execution limits and missing coverage; statement counts are never labeled
        people or organizations. Rendering has no I/O or model side effects.
    """
    lines = ['# Current answer (provisional)', '', answer['question'], '',
        'Extracted from reviewed evidence; not a new synthesis or a complete survey.',
        'Source support is not independent truth verification. Entity totals are not resolved.',
        f"Run: {answer['run_id']} | Status: {answer['status']} | Coverage: {answer['coverage']}"]
    if answer['stop_reason']:
        lines.append('Stopped: ' + answer['stop_reason'] +
            (' (' + ', '.join(answer['stop_details']) + ')' if answer['stop_details'] else ''))
    if answer['coverage'] != 'assessed':
        lines.append('Answer coverage has not been assessed; missing findings remain unknown.')
    if answer['integrity_excluded']:
        lines.append(f"Excluded {answer['integrity_excluded']} previously anchored claims: saved evidence no longer validates.")
    sections = [('findings', 'Supported answer findings'),
        ('counterevidence', 'Counterevidence and exclusions — do not ignore'),
        ('disputed', 'Disputed proposals — not accepted as findings')]
    if include_leads:
        sections.append(('leads', 'Leads — relevance, eligibility or support still unresolved'))
    for section, title in sections:
        rows = answer[section]
        if not rows and section != 'findings':
            continue
        lines.extend(['', '## ' + title, ''])
        if not rows:
            lines.append('No reviewed, goal-linked answer claims with intact evidence are available yet. This is not evidence that no answer exists.')
        for row in rows:
            references = ', '.join(c['candidate_id'] for c in row['citations'])
            lines.append(f"- {row['statement']} [{references}]")
            if section != 'findings':
                lines.append('  Review: ' + row['review_status'] + '; this is not an eligible-entity determination.')
            if row['goal_ids']:
                lines.append('  Goals: ' + '; '.join(g + ': ' + answer['goals'][g] for g in row['goal_ids']))
            for qualification in row['qualifications']:
                lines.append('  Qualification: ' + qualification)
            for source_id, location in dict.fromkeys((c['source_id'], c['location']) for c in row['citations']):
                lines.append(f'  Source {source_id}: {location}')
    lines.extend(['', f"Unresolved lead statements: {len(answer['leads'])}; not additional confirmed targets."])
    if not include_leads and answer['leads']:
        lines.append(f"Full leads: python -m projectclarity answer {answer['run_id']} --details (use the same --output-dir). New runs also save answer.md.")
    if answer['gaps']:
        lines.extend(['', '## Recorded gaps', ''])
        lines.extend('- ' + gap for gap in dict.fromkeys(answer['gaps']))
    lines.extend(['', 'Claim IDs refer to candidates.md / candidates.json. New CLI runs also save exact evidence spans in answer.json.'])
    return terminal_text('\n'.join(lines)) + '\n'


def read_saved_answer(output_root: Path, run_id: str) -> dict:
    """Rebuild an older run's answer without resuming or modifying its receipts.

    Args:
        output_root: Directory containing saved runs and their normalized sources.
        run_id: Single run identifier, checked by the existing safe-directory helper.

    Returns:
        Provisional answer data from the saved session and freshly checked evidence.
        Invalid directories, mismatched identities or unreadable snapshots raise a
        user-facing ResearchError. No network, model requests or writes occur.
    """
    from projectclarity.research_schemas import ResearchSession
    from projectclarity.nodes.plan_research.research_planner import ResearchError
    from projectclarity.run_status_commands import run_directory

    directory = run_directory(output_root, run_id)
    try:
        session = ResearchSession.model_validate_json((directory / 'session.json').read_text(encoding='utf-8'))
        if session.session_id != run_id:
            raise ValueError('Run identity mismatch')
        return build_provisional_answer(session, output_root)
    except (OSError, ValueError):
        raise ResearchError('Cannot build an answer from this saved session. Check its format and permissions.') from None
