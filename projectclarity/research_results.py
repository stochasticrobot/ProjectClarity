"""Build the run's shared result counts from recorded claims, not from prose.

Reports, dashboard projections and coverage call result_progress. A designed
report uses its declared identity fields and per-field evidence; a legacy plan
uses complete required-field tuples. Runs using separate inclusion review also
require evidence-backed scope, date and section-requirement decisions before a
field-supported entry counts as supported. Historical runs without that policy
retain field-only counts, explicitly labelled as eligibility not assessed. A name-only observation cannot fill a known
person's affiliation merely because the names match. Designed reports can route
partial contributions through an unambiguous, supported identity for the same
explicit subject in the same source-version and extraction batch. This module
filters by recorded support/roles but does not reread source files: callers pass
freshly verified candidate IDs when source-integrity checking is required.
No model requests, file writes or cross-run identity matching occur here."""

from collections import Counter
from projectclarity.result_identity import (ACCOUNTING_VERSION, result_key, result_id,
    person_labels, organization_labels, NAME_FIELDS, PERSON_FIELDS)
from projectclarity.goal_measurement import measure_goal
from projectclarity.result_contract import item_fields, PROVENANCE_FIELDS


def structure_status(claim):
    """Classify whether a proposed claim supplies its declared relationship structure.

    A statement can have a supporting quotation while omitting the structured entity
    relationship needed for result accounting. Keep that structural issue separate
    from factual support or review status.

    Args:
        claim: Serialized extraction proposal, including legacy proposals without a kind.

    Returns:
        A structure label, not a truth judgment. Legacy multi-entity prose is explicitly
        unclassified rather than guessed to be relational or silently counted as complete.
    """
    kind = claim.get('claim_kind')
    relationship = claim.get('relationship')
    if relationship:
        return 'structured' if kind != 'non_relational' else 'inconsistent_structure'
    if kind == 'relational':
        return 'needs_structure'
    if kind == 'non_relational':
        return 'non_relational'
    return 'needs_classification' if len(set(claim.get('entity_ids', []))) >= 2 else 'unclassified'


def record_check(record, goal):
    """Validate the field names and values of one proposed result against its goal.

    A result record might represent a researcher with name and affiliation fields.
    Report missing, unknown or duplicated fields instead of inferring values from
    nearby prose or other records.

    Args:
        record: Serialized goal-linked record with a list of named field values.
        goal: Serialized plan goal, optionally defining a result shape.

    Returns:
        Status, exact supplied values, missing fields and structural errors. Complete
        means the required fields exist, not that the values are true. Duplicate or
        unrecognized item field names are invalid; missing item fields remain incomplete.
        Reserved provenance values remain in the original proposal, not item identity.
        Evidence citations are supplied separately from validated candidate receipts.
    """
    shape = goal.get('result_shape')
    fields = [f for f in record.get('fields', []) if f['name'].casefold() not in PROVENANCE_FIELDS]
    names = [field['name'] for field in fields]
    values = {field['name']: field['value'] for field in fields}
    required = item_fields(shape)
    errors = []
    if not shape:
        errors.append('goal_has_no_result_shape')
    elif not required:
        errors.append('item_identity_undefined')
    if record['goal_id'] != goal['goal_id']:
        errors.append('wrong_result_goal')
    if len(names) != len(set(names)):
        errors.append('duplicate_result_field')
    if set(names) - set(required):
        errors.append('unknown_result_field')
    missing = [name for name in required if not values.get(name, '').strip()]
    return {'status': 'invalid' if errors else 'incomplete' if missing else 'complete',
        'values': values, 'missing_fields': missing, 'errors': errors}


def result_defects(claim, goals):
    """Identify missing relationship or result-field structure in one answer claim.

    Extraction uses these issue codes to decide whether a focused source reread may
    help. They describe structural omissions, not proof that the claim is false or
    that the missing information can be recovered.

    Args:
        claim: Serialized extraction proposal, retained unchanged by this check.
        goals: Serialized goals defining this run's optional result shapes.

    Returns:
        Distinct structural defect codes. No inference from prose, fabrication of
        records, truth judgment or model call occurs. An empty result-record list
        is permitted by the prompt for claims that do not identify an item; it does
        not itself trigger a retry or establish that the source contains no targets.
    """
    if 'answer_evidence' not in claim.get('research_roles', []):
        return []
    errors = []
    if structure_status(claim) in {'needs_structure', 'inconsistent_structure'}:
        errors.append('incomplete_relationship')
    shaped = {g['goal_id']: g for g in goals if g.get('result_shape') and g['goal_id'] in claim['goal_ids']}
    records = claim.get('result_records', [])
    if claim.get('claim_kind') is None and records and not claim.get('relationship'):
        errors.append('missing_claim_classification')
    for gid, goal in shaped.items():
        matches = [r for r in records if r['goal_id'] == gid]
        if any(record_check(r, goal)['status'] != 'complete' for r in matches):
            errors.append('incomplete_result_record')
    return sorted(set(errors))


def result_inventory(goal, candidates, person_values=(), organization_values=()):
    """Group result records for one research goal and retain their evidence statuses.

    Repeated observations of the same complete identifying fields should count once.
    Incomplete records and conflicting support remain visible rather than being joined
    by guessed identity. Designed reports use their separate declared-field inventory.

    Args:
        goal: One serialized goal with a non-null result shape.
        candidates: Run-local, anchored answer candidates associated with this goal.
        person_values: Run-local labels explicitly typed as people, for display-order normalization.
        organization_values: Explicit organization labels protected from person formatting.

    Returns:
        Records and counts partitioned into supported, pending, disputed, incomplete
        and invalid. Only supported claims with usable structure yield supported
        records when marked answer evidence. Explicit item records in lead-only
        claims stay pending even if the underlying qualified statement is supported.
        Case/Unicode/whitespace and explicit person display-order variants merge
        only when all goal fields match. Differing field values remain separate;
        this is not fuzzy identity or alias resolution. Named entities with differing
        field tuples remain visible as identity_pending rather than inflating distinct
        target counts; their claim evidence statuses are preserved. Incomplete
        or invalid observations remain individually visible because they cannot
        establish identity; fragments are never joined to invent a complete record.
        Qualifications, candidate IDs and anchored evidence provenance are retained
        separately from item values. No mutation or I/O occurs.
    """
    groups = {}
    missing = 0
    for candidate in candidates:
        claim = candidate['proposal']
        verdict = candidate.get('adjudication_status', 'pending')
        if verdict not in {'supported', 'pending', 'needs_review', 'insufficient_evidence', 'contradicted'}:
            continue
        records = [r for r in claim.get('result_records', []) if r['goal_id'] == goal['goal_id']]
        if not records:
            missing += 1
        for index, record in enumerate(records):
            check = record_check(record, goal)
            identity = result_key(check['values'], person_values, organization_values)
            key = (check['status'], identity)
            if check['status'] != 'complete':
                key += (candidate['candidate_id'], index)
            row = groups.setdefault(key, {**check, 'result_id': result_id(goal['goal_id'], key),
                'field_variants': [], 'candidate_ids': [], 'qualifications': [],
                'provenance': [], 'verdicts': set(), 'usable_support': False})
            if check['values'] not in row['field_variants']:
                row['field_variants'].append(check['values'])
            row['candidate_ids'].append(candidate['candidate_id'])
            for evidence in candidate.get('evidence', []):
                citation = {'candidate_id': candidate['candidate_id'], **evidence}
                if citation not in row['provenance']:
                    row['provenance'].append(citation)
            row['qualifications'].extend(claim.get('qualifications', []))
            row['verdicts'].add(verdict)
            if (verdict == 'supported' and 'answer_evidence' in claim.get('research_roles', [])
                    and structure_status(claim) in {'structured', 'non_relational'}):
                row['usable_support'] = True
    for row in groups.values():
        if row['status'] == 'complete':
            row['status'] = ('disputed' if 'contradicted' in row['verdicts'] else
                'supported' if row['usable_support'] else 'pending')
        row.pop('verdicts')
        row.pop('usable_support')
        row['candidate_ids'] = list(dict.fromkeys(row['candidate_ids']))
        row['qualifications'] = list(dict.fromkeys(row['qualifications']))
    # Differing attributes of an explicitly named person/organization might be
    # one entity with multiple affiliations, or homonyms. Neither silently merge
    # them nor certify two distinct targets. Arbitrary event/relationship fields
    # keep the plan's full-tuple identity and do not inherit this name heuristic.
    names = [f for f in item_fields(goal['result_shape']) if f.casefold() in NAME_FIELDS]
    possible = {}
    if len(names) == 1:
        field = names[0]
        for row in groups.values():
            if row['status'] not in {'supported', 'pending'}:
                continue
            key = dict(result_key(row['values'], person_values, organization_values))[field]
            if (field.casefold() in PERSON_FIELDS | {'organization_name', 'organisation_name'}
                    or key in person_values or key in organization_values):
                possible.setdefault(key, []).append(row)
    for matches in possible.values():
        if len(matches) > 1:
            for row in matches:
                row['evidence_status'] = row['status']
                row['status'] = 'identity_pending'
                row['identity_note'] = 'Same name has different required fields; distinct identity is unresolved.'
    counts = Counter(row['status'] for row in groups.values())
    return {'unit': goal['result_shape']['unit'], 'required_fields': item_fields(goal['result_shape']),
        'count_basis': ACCOUNTING_VERSION,
        'identified': counts['supported'] + counts['pending'],
        'observed': counts['supported'] + counts['pending'] + counts['disputed'] + counts['identity_pending'],
        'identity_pending': counts['identity_pending'],
        'incomplete_count_basis': 'individual_observations_not_distinct_targets',
        **{status: counts[status] for status in ('supported', 'pending', 'disputed', 'incomplete', 'invalid')},
        'claims_without_records': missing, 'records': list(groups.values())}


def result_progress(session, valid_ids=None):
    """Compute shared per-goal claim and result counts for reports, coverage and the dashboard.

    Select this run's eligible observations, group records using their declared fields
    or answer specification, and retain missing structure and mapping gaps separately.
    Counting a supported claim is not the same as counting a complete answer entry.

    Args:
        session: Serialized snapshot of one run; no sibling directories or stores are read.
        valid_ids: Optional set of candidate IDs freshly checked against saved sources.
            None uses recorded anchor status for live display, not a new integrity check.

    Returns:
        Shared mapping ledger plus current candidate/structure counts and per-goal
        record inventories when the
        plan defines a result shape. Fact counts are not target counts; record counts
        use explicit report identities when designed, otherwise legacy full-field
        tuples. Report fields combine by identical complete identities or explicit
        same-source subject associations and retain per-field support/conflicts. Unsupported, out-of-scope
        and invalid proposals are never promoted. Explicit lead item records remain
        visible as pending in legacy inventories. Versioned answer runs instead
        retain pure leads in evidence without making them mandatory report entries.
        No state is changed.
    """
    candidates = (session.get('extraction') or {}).get('candidates', [])
    goals = (session.get('plan') or {}).get('goals', [])
    person_values = person_labels(session.get('extraction'))
    organization_values = organization_labels(session.get('extraction'))
    rows = []
    from projectclarity.nodes.schedule_work.answer_controller import POLICY
    scoped = (session.get('workflow', {}).get('controller_policy') == POLICY
              and bool((session.get('answer_schema') or {}).get('completion_checks')))
    report_sections = {s['goal_id']: s for s in (session.get('answer_schema') or {}).get('sections', [])}
    for goal in goals:
        relevant = [c for c in candidates if goal['goal_id'] in c['proposal']['goal_ids']
            and ('answer_evidence' in c['proposal'].get('research_roles', [])
                or any(r['goal_id'] == goal['goal_id'] for r in c['proposal'].get('result_records', [])))
            and c['evidence_status'] == 'anchored'
            and (valid_ids is None or c['candidate_id'] in valid_ids)]
        supported = [c for c in relevant if c.get('adjudication_status') == 'supported'
            and 'answer_evidence' in c['proposal'].get('research_roles', [])]
        structures = Counter(structure_status(c['proposal']) for c in supported)
        from projectclarity.answer_schema.inventory import report_inventory
        section = report_sections.get(goal['goal_id'])
        inventory = (report_inventory(goal, relevant, section, answer_scope_only=scoped, qualification_data=session) if section and section['fields'] else
                     result_inventory(goal, relevant, person_values, organization_values) if goal.get('result_shape') else None)
        rows.append({'goal_id': goal['goal_id'], 'question': goal['question'],
            'report_section': section is not None if session.get('answer_schema') else None,
            'results': inventory,
            'supported_facts': len(supported),
            'pending_facts': sum(c.get('adjudication_status', 'pending') in
                {'pending', 'needs_review', 'insufficient_evidence'} for c in relevant),
            'structured_facts': structures['structured'],
            'needs_structure': structures['needs_structure'] + structures['inconsistent_structure'],
            'needs_classification': structures['needs_classification'] + structures['unclassified'],
            'non_relational_facts': structures['non_relational']})
    for row, goal in zip(rows, goals):
        row['measurement'] = measure_goal(session, goal, row['results'], valid_ids)
    from projectclarity.claim_review.report_mapping import mapping_ledger
    return {'mapping_ledger': mapping_ledger(session, valid_ids),
        'run_id': session.get('session_id'), 'count_basis': 'candidate_facts_not_distinct_targets',
        'candidate_count': len(candidates), 'goals': rows}


def progress_line(goal):
    """Describe goal yield using its actual count basis for terminal and final output.

    Args:
        goal: One per-goal row returned by result_progress.

    Returns:
        A human-readable line distinguishing complete result records, incomplete
        observations and facts. It does not assert resolved identities or coverage.
    """
    results = goal['results']
    if results is not None:
        m = goal.get('measurement', {})
        target = (f" Target: {m['target']} ({m['target_basis']}); "
            f"{m['found_percent']:.1f}% identified, {m['supported_percent']:.1f}% supported."
            if m.get('found_percent') is not None else
            ' Target: 0; an empty-result target has no percentage ratio.' if m.get('target') == 0 else
            ' Target unknown; no completeness percentage.')
        incomplete_unit = 'records' if results['count_basis'] == 'answer-schema-v1' else 'observations'
        unassigned = (f"{results.get('unassigned_count', 0)} unassigned observations (not report entries). "
                      if results['count_basis'] == 'answer-schema-v1' else '')
        return (f"{goal['goal_id']} · {results['unit']}: {results['identified']} identified; {results['supported']} supported complete records; "
            f"{results['pending']} pending; {results['disputed']} disputed; "
            f"{results['identity_pending']} observations awaiting identity resolution; {results['incomplete']} incomplete {incomplete_unit}; {results['invalid']} invalid observations. "
            + unassigned + f"{results['claims_without_records']} answer claims without item records (not counted as targets). "
            + ('Entries share explicit identity fields; each required field keeps its evidence status.'
             if results['count_basis'] == 'answer-schema-v1' else
             'Repeated evidence and explicit name-format variants count once when all required fields match.') + target)
    return (f"{goal['goal_id']}: {goal['supported_facts']} supported answer facts; "
        f"{goal['pending_facts']} pending; {goal['structured_facts']} relationally structured; "
        f"{goal['needs_structure']} need structure; {goal['needs_classification']} unclassified. "
        'These are facts, not distinct targets.')
