"""Build and save a readable answer from the run's existing claims and report entries.

The final report and the answer inspection command use these helpers to recheck
saved source evidence, separate supported findings from unresolved claims, and
render statements, qualifications and citations. When an answer specification
exists, report entries are rendered according to its sections and fields. The
module does not ask a model to write new conclusions or guess that similar names
refer to the same person. A matching quotation alone does not make a claim supported.
Reading a saved answer and saving final exports are separate entry points.
"""

from pathlib import Path
import json

from projectclarity.nodes.assess_research_gaps.node import checked_candidates
from projectclarity.plan_renderer import terminal_text
from projectclarity.coverage.freshness import coverage_freshness
from projectclarity.research_results import result_progress, structure_status, progress_line, result_defects


def build_provisional_answer(session, output_root: Path) -> dict:
    """Build answer data by separating supported claims from unresolved or invalid evidence.

    Recheck saved quotations and source versions, then prepare report entries, findings
    and limitations without requesting new model prose. This permits a provisional
    answer even when the run has no model allowance left.

    Args:
        session: Typed run snapshot containing the question, goals and review results.
        output_root: Parent of the run directory; saved source text is read only.

    Returns:
        Plain application-owned data with a shared mapping ledger in progress,
        supported answer findings, counterevidence,
        disputed claims and leads. Exact statement/qualification/goal duplicates
        merge citations, not identities. Missing/altered evidence is excluded;
        stale coverage gaps are separated from current results; no final model call
        is needed. Malformed extraction contracts raise validation errors rather than inventing
        an answer. No session mutation, network requests or file writes occur.
    """
    goals = {g.goal_id: g.question for g in session.plan.goals} if session.plan else {}
    freshness = coverage_freshness(session)
    historical_gaps = [gap for item in session.coverage.get('criteria_progress', [])
                       for gap in item.get('gaps', [])]
    current = freshness['status'] == 'fresh'
    answer = dict(question=session.request.question, run_id=session.session_id,
        status=session.status, stop_reason=session.workflow.get('stop_reason'),
        stop_details=session.workflow.get('stop_details', []), goals=goals,
        coverage=('assessed' if current else freshness['status']),
        coverage_freshness=freshness,
        gaps=historical_gaps if current else [],
        historical_gaps=[] if current else historical_gaps,
        findings=[], counterevidence=[], disputed=[], leads=[], integrity_excluded=0,
        answer_schema=session.answer_schema)
    if not session.extraction:
        return answer
    record, valid = checked_candidates(session, output_root)
    answer['progress'] = result_progress(session.model_dump(mode='json'), set(valid))
    answer['integrity_excluded'] = sum(c.evidence_status == 'anchored'
        and c.candidate_id not in valid for c in record.candidates)
    documents = {d.source_id: d for d in session.documents}
    merged = {}
    for candidate in valid.values():
        proposal = candidate.proposal
        roles = set(proposal.research_roles)
        goal_ids = sorted(set(proposal.goal_ids) & goals.keys())
        verdict = candidate.adjudication_status
        if not goal_ids:
            continue
        elif verdict == 'supported' and 'answer_evidence' in roles:
            section = 'findings'
        elif verdict == 'supported' and 'counterevidence' in roles:
            section = 'counterevidence'
        elif verdict == 'contradicted' and roles & {'answer_evidence', 'lead'}:
            section = 'disputed'
        elif verdict in {'supported', 'pending', 'needs_review', 'insufficient_evidence'} and roles & {'answer_evidence', 'lead'}:
            section = 'leads'
        else:
            continue
        proposal_data = proposal.model_dump(mode='json')
        # Different structures/records must not disappear behind equal prose.
        key = (section, ' '.join(proposal.statement.split()), tuple(proposal.qualifications),
               tuple(goal_ids), verdict, structure_status(proposal_data),
               json.dumps(proposal_data['relationship'], sort_keys=True),
               json.dumps(proposal_data['result_records'], sort_keys=True))
        if key not in merged:
            row = dict(statement=proposal.statement, qualifications=list(proposal.qualifications),
                goal_ids=goal_ids, review_status=verdict, research_roles=sorted(roles),
                structure_status=structure_status(proposal_data),
                result_records=proposal_data['result_records'],
                structure_issues=result_defects(proposal_data, [g.model_dump() for g in session.plan.goals]),
                citations=[])
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
        execution limits, unresolved claim-to-report mappings and missing coverage;
        designed report entries precede progress and mapping diagnostics.
        Statement counts are never labeled
        people or organizations. Rendering has no I/O or model side effects.
    """
    lines = ['# Current answer (provisional)', '', answer['question'], '',
        'Extracted from reviewed evidence; not a new synthesis or a complete survey.',
        'Source support is not independent truth verification. Entity totals are not resolved.',
        f"Run: {answer['run_id']} | Status: {answer['status']} | Coverage: {answer['coverage']}"]
    if answer['stop_reason']:
        lines.append('Stopped: ' + answer['stop_reason'] +
            (' (' + ', '.join(answer['stop_details']) + ')' if answer['stop_details'] else ''))
    if answer['coverage'] in {'stale', 'freshness_unknown'}:
        freshness = answer['coverage_freshness']
        lines.append('Coverage assessment is OUTDATED or its freshness cannot be verified. '
            f"Assessment snapshot: {freshness['assessed_candidate_count']} candidates; "
            f"current run: {freshness['current_candidate_count']}. Old gaps are not current counts.")
    elif answer['coverage'] != 'assessed':
        lines.append('Answer coverage has not been assessed; missing findings remain unknown.')
    if answer['integrity_excluded']:
        lines.append(f"Excluded {answer['integrity_excluded']} previously anchored claims: saved evidence no longer validates.")
    from projectclarity.answer_schema.render import render_report
    lines.extend(render_report(answer))
    for goal in answer.get('progress', {}).get('goals', []):
        lines.append(progress_line(goal))
    mapping = answer.get('progress', {}).get('mapping_ledger', {})
    gaps = [r for r in mapping.get('rows', []) if r['blocking']]
    if gaps:
        lines.extend(['', '## Unresolved claim-to-report mappings', '',
            'Supported facts below are not yet accounted for in the report. Completion is blocked.'])
        for gap in gaps:
            lines.append(f"- {gap['candidate_id']} / {gap['goal_id']}: {gap['status']}; "
                + ', '.join(gap['field_names']) + '. ' + gap['rationale'])
    sections = [('findings', 'Supported answer findings'),
        ('counterevidence', 'Counterevidence and exclusions — do not ignore'),
        ('disputed', 'Disputed proposals — not accepted as findings')]
    if include_leads:
        sections.append(('leads', 'Leads — relevance, eligibility or support still unresolved'))
    for section, title in sections:
        if section == 'findings' and answer.get('answer_schema'):
            continue
        rows = answer[section]
        if not rows and section != 'findings':
            continue
        lines.extend(['', '## ' + title, ''])
        if not rows:
            lines.append('No reviewed, goal-linked answer claims with intact evidence are available yet. This is not evidence that no answer exists.')
        for row in rows:
            references = ', '.join(c['candidate_id'] for c in row['citations'])
            lines.append(f"- {row['statement']} [{references}]")
            if 'counterevidence' in row.get('research_roles', []):
                lines.append('  Also provides counterevidence; this role does not mean the claim is false.')
            if row.get('structure_issues'):
                lines.append('  Incomplete structure: ' + ', '.join(row['structure_issues']) + '.')
            for result in row.get('result_records', []):
                fields = '; '.join(f"{field['name']}: {field['value']}" for field in result['fields'])
                lines.append(f"  Result fields ({result['goal_id']}): {fields or '[none supplied]'}")
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
    if include_leads and answer.get('historical_gaps'):
        lines.extend(['', '## Historical assessment gaps — not current conclusions', ''])
        lines.extend('- ' + gap for gap in dict.fromkeys(answer['historical_gaps']))
    lines.extend(['', 'Claim IDs refer to candidates.md / candidates.json. New research runs also save exact evidence spans in answer.json.'])
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


def save_provisional_answer(session, output_root: Path) -> None:
    """Persist final answer exports before the workflow announces terminal status.

    Args:
        session: Finalized or paused run with its authoritative evidence receipts.
        output_root: Parent directory of the existing run; no model calls are made.
    Returns:
        None. Writes answer.md and answer.json with full evidence-linked leads.
        Validation and I/O failures propagate so finalization cannot announce a
        successful durable export that did not happen. Source receipts are unchanged.
    """
    from projectclarity.session_storage import write_text
    answer = build_provisional_answer(session, output_root)
    directory = output_root / session.session_id
    write_text(directory / 'answer.md', render_provisional_answer(answer, include_leads=True))
    write_text(directory / 'answer.json', json.dumps(answer, indent=2, ensure_ascii=False) + '\n')
