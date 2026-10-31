"""Track whether supported claims have explicit fields in the requested report.

Adjudication uses queue_mapping_repairs to request a bounded reread of the original
passage when the reviewer marks a contribution needs_mapping. Reports, coverage
and dashboard projections use mapping_ledger to derive the same unresolved-gap
list from saved claims, reviews and repair ownership. No prose is parsed into
fields. A repair is not successful merely because its model call completed.

Only answer-evidence claims create report obligations. Complete identity may be
established by a supported same-batch subject association, using the same rule as
report assembly. Each goal is checked independently; an unrelated assessment does
not invalidate correctly attributed contributions for another goal.
Missing legacy assessments stay unknown. Ledger projection performs no I/O;
queueing mutates the supplied session and extraction record for the caller to save."""
import hashlib

from projectclarity.research_results import record_check, structure_status

MAPPING_VERSION = 'report-mapping-v1'


def mapping_assessments(data, candidate, identity_candidates=None):
    """Validate the reviewer's account of how claims contribute to report fields.

    A supported claim can still lack the structured fields needed for a report entry.
    These checks verify the reviewer's claim, goal and field references separately
    from whether the quoted source supports the claim.

    Args:
        data: Serialized session with plan goals, optional answer-schema sections and
            claim_reviews keyed by candidate ID; no file reads are performed.
        candidate: Serialized extraction candidate with its proposal, source-text hash
            and candidate ID. Neither this dictionary nor data is modified.
        identity_candidates: Optional source-verified candidate list for same-batch
            identity association. None uses the supplied session candidates.
    Returns:
        One assessment dictionary per shaped goal named in the claim. Each contains
        goal_id, status, field_names and rationale. Missing, stale or invalid review
        data produces unknown rather than a guessed mapping. A mapped decision needs
        an answer_evidence role, usable claim structure, and one valid result record
        containing the named fields and a complete or explicitly associated identity.
        Other required attributes need not be present in that contribution. Pure
        context/leads return not_applicable, regardless of a mistaken model mapping.
        Invalid or duplicate judgments affect only their own goal.
        Input dictionaries are expected to follow the persisted session schema.
    """
    proposal = candidate['proposal']
    goals = {g['goal_id']: g for g in (data.get('plan') or {}).get('goals', [])
             if g.get('result_shape') and g['goal_id'] in proposal['goal_ids']}
    sections = {s['goal_id']: s for s in (data.get('answer_schema') or {}).get('sections', [])}
    review = data.get('claim_reviews', {}).get(candidate['candidate_id'], {})
    assessment = review.get('assessment') or {}
    supplied = assessment.get('report_mappings')
    malformed = (not isinstance(supplied, list) or review.get('error_code')
                 or review.get('text_sha256') != candidate.get('text_sha256'))
    supplied = supplied if isinstance(supplied, list) else []
    from projectclarity.answer_schema.source_identity import source_identity
    identity_candidates = (data.get('extraction') or {}).get('candidates', []) if identity_candidates is None else identity_candidates
    rows = []
    for gid, goal in goals.items():
        if 'answer_evidence' not in proposal.get('research_roles', []):
            rows.append(dict(goal_id=gid, status='not_applicable', field_names=[],
                rationale='This claim is retained as background, a lead or counterevidence, not an answer contribution.'))
            continue
        matches = [r for r in supplied if isinstance(r, dict) and r.get('goal_id') == gid]
        row = dict(goal_id=gid, status='unknown', field_names=[], rationale='Mapping assessment missing or invalid.')
        if not malformed and len(matches) == 1:
            item = matches[0]
            fields = item.get('field_names', [])
            allowed = set(goal['result_shape']['required_fields'])
            status = item.get('status')
            valid = (isinstance(fields, list) and all(isinstance(f, str) for f in fields)
                     and len(fields) == len(set(fields)) and set(fields) <= allowed
                     and status in {'mapped', 'needs_mapping', 'identity_unresolved', 'not_applicable'}
                     and (bool(fields) or status == 'not_applicable'))
            if status == 'not_applicable' and fields:
                valid = False
            if valid and status == 'mapped':
                section = sections.get(gid, {})
                identity = section.get('identity_fields', list(allowed))
                records = [record_check(r, goal) for r in proposal.get('result_records', []) if r['goal_id'] == gid]
                valid = (structure_status(proposal) in {'structured', 'non_relational'}
                         and 'answer_evidence' in proposal.get('research_roles', [])
                         and any(not r['errors']
                             and all(r['values'].get(f, '').strip() for f in fields)
                             and (all(r['values'].get(f, '').strip() for f in identity)
                                  or section and source_identity(candidate, r['values'], identity_candidates, goal, section))
                             for r in records))
            if valid:
                row = dict(item)
        rows.append(row)
    return rows


def closes_gap(original, replacement, gap, assessment):
    """Check whether a reread supplied the report contribution missing from an earlier claim.

    Preserve source anchors and qualifications, then compare explicit field values
    rather than demanding identical prose for an already structured contribution.
    Claims with no original field values still require identical wording: source
    overlap alone cannot prove that a paraphrase means the same thing.

    Args:
        original: Supported original candidate, retained without modification.
        replacement: Independently reviewed candidate from its bounded child attempt.
        gap: Original goal and affected field names.
        assessment: Structurally validated child mapping assessment.
    Returns:
        True for a supported child mapping with all affected fields present, unchanged
        original structured values (or identical unstructured wording), unchanged
        qualifications and covering anchors from the same source version. No semantic
        equivalence is inferred from free prose. A completed call alone returns no credit.
    """
    left, right = original['proposal'], replacement['proposal']
    from projectclarity.result_identity import normalized
    before = [{f['name']: f['value'] for f in r['fields']}
              for r in left.get('result_records', []) if r['goal_id'] == gap['goal_id']]
    after = [{f['name']: f['value'] for f in r['fields']}
             for r in right.get('result_records', []) if r['goal_id'] == gap['goal_id']]
    complete = [new for new in after if all(new.get(f, '').strip() for f in gap['field_names'])]
    continuity = (all(old and any(all(k in new and normalized(v) == normalized(new[k])
                        for k, v in old.items()) for new in complete) for old in before)
                  if before else bool(complete) and left['statement'] == right['statement'])
    return bool(assessment['status'] == 'mapped'
        and assessment['goal_id'] == gap['goal_id']
        and continuity
        and {normalized(q) for q in left.get('qualifications', [])}
            == {normalized(q) for q in right.get('qualifications', [])}
        and original.get('source_id') == replacement.get('source_id')
        and original.get('text_sha256') == replacement.get('text_sha256')
        and original.get('evidence') and all(any(
            e['start'] <= old['start'] and old['end'] <= e['end']
            for e in replacement.get('evidence', [])) for old in original['evidence']))


def mapping_ledger(data, valid_ids=None):
    """List supported claims whose expected report contributions are still unresolved.

    Combine saved review decisions and repair records into the shared gap list shown
    by reports, coverage assessment and the dashboard. Lead and background claims
    are retained as not_applicable, never mandatory report contributions.

    Args:
        data: Serialized session containing extraction candidates/attempts, claim
            reviews, plan/schema and workflow.report_mapping_repairs. Not modified.
        valid_ids: Optional collection of freshly source-verified candidate IDs.
            None trusts recorded anchored status; an empty collection accepts none.
    Returns:
        Dictionary with version, rows and unresolved count. Rows cover supported,
        anchored claims associated with shaped goals, including lead/context claims.
        Each row retains the original assessment, current gap state, candidate/source
        links, repair attempt and any closing child IDs. A queued/started child or
        child awaiting adjudication is pending; a finished child without a qualifying
        replacement is exhausted. Only supported children meeting closes_gap's
        field/qualification/source continuity checks resolve a gap. Unknown and
        identity-unresolved answer contributions also block.
        This is a derived view, not a newly persisted ledger: no file writes, calls,
        retroactive scheduling or changes to original receipts occur. Attempts without
        batch IDs cannot establish ownership of a repair.
    """
    extraction = data.get('extraction') or {}
    candidates = extraction.get('candidates', [])
    attempts = {a['batch_id']: a for a in extraction.get('attempts', []) if a.get('batch_id')}
    repairs = data.get('workflow', {}).get('report_mapping_repairs', {})
    supported = [c for c in candidates if c.get('adjudication_status') == 'supported'
        and c.get('evidence_status') == 'anchored' and (valid_ids is None or c['candidate_id'] in valid_ids)]
    assessments = {c['candidate_id']: mapping_assessments(data, c, supported) for c in supported}
    rows = []
    for candidate in supported:
        cid = candidate['candidate_id']
        repair = repairs.get(cid, {})
        bid = repair.get('batch_id')
        for item in assessments[cid]:
            status = item['status']
            resolved_by = []
            if status == 'needs_mapping' and bid:
                for child in supported:
                    if child.get('batch_id') != bid:
                        continue
                    if any(closes_gap(candidate, child, item, a) for a in assessments[child['candidate_id']]):
                        resolved_by.append(child['candidate_id'])
                attempt = attempts.get(bid, {})
                child_candidates = [c for c in candidates if c.get('batch_id') == bid]
                waiting_review = any(c.get('adjudication_status', 'pending') == 'pending' for c in child_candidates)
                status = ('resolved' if resolved_by else 'pending' if
                    attempt.get('status') in {'not_attempted', 'started'} or waiting_review else 'exhausted')
            elif status == 'needs_mapping' and repair.get('status') == 'exhausted':
                status = 'exhausted'
            rows.append({**item, 'assessment_status': item['status'], 'status': status,
                'candidate_id': cid, 'source_id': candidate.get('source_id'),
                'text_sha256': candidate.get('text_sha256'), 'parent_batch_id': candidate.get('batch_id'),
                'repair_batch_id': bid, 'resolved_by': resolved_by,
                'blocking': status not in {'mapped', 'not_applicable', 'resolved'}})
    return dict(version=MAPPING_VERSION, rows=rows, unresolved=sum(r['blocking'] for r in rows))


def queue_mapping_repairs(session, record):
    """Add focused extraction attempts for supported claims missing expected report fields.

    Each attempt points back to the original source passage and missing contribution.
    This function records the requested repair for later scheduling; it does not call
    a model, guarantee execution before the deadline or declare the gap resolved.

    Args:
        session: Typed adjudicated run; workflow.report_mapping_repairs is updated
            in place to record each candidate's repair ownership or exhaustion.
        record: Typed ExtractionRecord just validated against saved source text.
            New ExtractionAttempts are appended to this mutable record, not executed.
    Returns:
        None. Candidates marked needs_mapping share one exact-parent-passage child,
        reusing a structural refinement with identical offsets when present. Existing
        ownership is left unchanged, including finished/uncertain attempts. A missing
        parent or a parent that is itself a refinement is marked exhausted; recursive
        repairs are not created. Unknown and identity_unresolved decisions are not
        queued. The caller must persist the session/record; this function performs no
        I/O, reserves no model request and does not guarantee scheduler execution.
    """
    from projectclarity.extraction.extraction_contracts import ExtractionAttempt
    data = session.model_dump(mode='json')
    data['extraction'] = record.model_dump(mode='json')
    attempts = {a.batch_id: a for a in record.attempts}
    repairs = session.workflow.setdefault('report_mapping_repairs', {})
    for candidate in record.candidates:
        if candidate.adjudication_status != 'supported' or candidate.evidence_status != 'anchored':
            continue
        cid = candidate.candidate_id
        if cid in repairs or not any(a['status'] == 'needs_mapping'
                for a in mapping_assessments(data, candidate.model_dump(mode='json'))):
            continue
        parent = attempts.get(candidate.batch_id)
        if not parent or parent.refinement_parent:
            repairs[cid] = dict(status='exhausted', batch_id=None)
            continue
        existing = next((a for a in record.attempts if a.refinement_parent == parent.batch_id
                         and a.start == parent.start and a.end == parent.end), None)
        if existing is None:
            token = hashlib.sha256((MAPPING_VERSION + ':' + parent.batch_id).encode()).hexdigest()[:20]
            pid = 'M' + token
            existing = ExtractionAttempt(batch_id=f'{parent.source_id}_{pid}', source_id=parent.source_id,
                passage_id=pid, text_sha256=parent.text_sha256, start=parent.start, end=parent.end,
                refinement_parent=parent.batch_id)
            record.attempts.append(existing)
            attempts[existing.batch_id] = existing
        repairs[cid] = dict(status='queued', batch_id=existing.batch_id)
