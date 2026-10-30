"""Combine extracted field values into the entries shown in a designed report.

An observation is a proposed set of field values backed by a source claim. A report
entry groups observations only when every identity field declared by the answer
specification is present and matches after limited formatting normalization.
For example, if identity requires name and affiliation, a name-and-topic observation
cannot be joined to a row merely because that row has the same name. It remains
unassigned evidence.

Each field keeps its own support status. The rules for required fields and whether
a field permits one or several values determine the entry's overall status.
Reports, dashboard counts and coverage assessment share this inventory through
result_progress. This module processes supplied data without reading sources or
calling a model. It does not infer missing fields or independently establish that
an entry meets the research question. Claim-to-report mapping checks are separate
and can still block completion for observations excluded here.
"""
from collections import Counter
from projectclarity.result_identity import normalized, person_label, result_id

COUNT_BASIS = 'answer-schema-v1'
AGGREGATION_VERSION = 'record-fields-v2'


def field_key(value, kind):
    """Create a comparison key for a report field using its declared value type.

    This allows formatting variants of the same value to group together. Only a field
    declared as a person name receives person-name order normalization; the function
    does not infer identity from similar spelling.

    Args:
        value: Source-backed string proposed for a report field.
        kind: The report's value_kind, not an extractor's free-text entity type.
    Returns:
        Comparable text; only person_name reverses explicit comma name order.
        Organization names, dates, numbers and arbitrary text retain punctuation.
    """
    return person_label(value) if kind == 'person_name' else normalized(value)


def report_inventory(goal, candidates, section, *, answer_scope_only=False):
    """Build report entries by grouping observations with complete matching identity fields.

    For example, a section identified by name and affiliation combines field values
    only when both identifiers agree. A name-only observation remains unassigned even
    if one existing row has that name. Each field retains its alternatives and evidence
    status; the section's single-value or multiple-value rule determines whether those
    alternatives leave the entry unresolved.

    Args:
        goal: Serialized research goal bound to this report section.
        candidates: Run-local, source-anchored candidates, optionally freshly checked.
        section: Validated report section with field definitions and identity fields.
        answer_scope_only: Versioned policy excluding pure leads/context from answer
            obligations; counterevidence still attaches to matching answer identities.
    Returns:
        Shared report/coverage inventory. Missing identity remains an uncounted
        observation in unassigned_observations, never an additional report row or
        a source of inferred missing identity values. Known identities with missing attributes remain incomplete
        entries. Each field retains its own support and provenance; conflicting
        values in single-valued fields remain pending, not automatically disputed.
        Multiple-valued fields collect supported alternatives. Explicit contradicted
        assessments dispute entries; a counterevidence role alone does not establish
        contradiction. Unassigned observations remain visible to coverage but are
        excluded from report counts and automatic field-completeness blockers.
        Pure leads remain in the evidence store under the versioned policy, not as
        mandatory report rows. No fuzzy aliases or inferred field values are added.
    """
    from projectclarity.research_results import record_check, structure_status
    fields = {f['name']: f for f in section['fields']}
    identity_fields = section['identity_fields']
    groups, unassigned, missing = {}, {}, 0
    answer_identities = set()
    if answer_scope_only:
        for candidate in candidates:
            if 'answer_evidence' not in candidate['proposal'].get('research_roles', []):
                continue
            for record in candidate['proposal'].get('result_records', []):
                if record['goal_id'] != goal['goal_id']:
                    continue
                values = record_check(record, goal)['values']
                if all(values.get(f, '').strip() for f in identity_fields):
                    answer_identities.add(tuple((f, field_key(values[f], fields[f]['value_kind']))
                        for f in identity_fields))
    for candidate in candidates:
        claim = candidate['proposal']
        roles = claim.get('research_roles', [])
        if answer_scope_only and not set(roles) & {'answer_evidence', 'counterevidence'}:
            continue
        verdict = candidate.get('adjudication_status', 'pending')
        if verdict not in {'supported', 'pending', 'needs_review', 'insufficient_evidence', 'contradicted'}:
            continue
        records = [r for r in claim.get('result_records', []) if r['goal_id'] == goal['goal_id']]
        missing += not records
        for index, record in enumerate(records):
            checked = record_check(record, goal)
            values = checked['values']
            identified = checked['status'] != 'invalid' and all(values.get(f, '').strip() for f in identity_fields)
            identity = tuple((f, field_key(values[f], fields[f]['value_kind'])) for f in identity_fields) if identified else (
                ('unresolved', candidate['candidate_id'], index),)
            if answer_scope_only and 'answer_evidence' not in roles and identity not in answer_identities:
                continue
            destination = groups if identified else unassigned
            row = destination.setdefault(identity, dict(result_id=result_id(goal['goal_id'], (COUNT_BASIS, identity)),
                values={}, field_variants=[], candidate_ids=[], qualifications=[], provenance=[],
                fields={f: {} for f in fields}, identity_complete=identified, errors=[], invalid=False))
            row['invalid'] |= checked['status'] == 'invalid'
            row['errors'].extend(checked['errors'])
            row['candidate_ids'].append(candidate['candidate_id'])
            row['qualifications'].extend(claim.get('qualifications', []))
            if values not in row['field_variants']:
                row['field_variants'].append(values)
            usable = (verdict == 'supported' and 'answer_evidence' in claim.get('research_roles', [])
                      and structure_status(claim) in {'structured', 'non_relational'})
            for evidence in candidate.get('evidence', []):
                row['provenance'].append({'candidate_id': candidate['candidate_id'], **evidence})
            for name, value in values.items():
                if name not in fields or not value.strip():
                    continue
                key = field_key(value, fields[name]['value_kind'])
                option = row['fields'][name].setdefault(key, dict(value=value, candidate_ids=[], supported=False, contradicted=False, unresolved_counterevidence=False))
                option['candidate_ids'].append(candidate['candidate_id'])
                option['supported'] |= usable
                option['contradicted'] |= verdict == 'contradicted'
                option['unresolved_counterevidence'] |= ('counterevidence' in roles and 'answer_evidence' not in roles
                    and verdict != 'contradicted')
    for row in [*groups.values(), *unassigned.values()]:
        row['field_status'] = {}
        row['display_values'] = {}
        for name, options in row.pop('fields').items():
            supported = [o for o in options.values() if o['supported'] and not o['contradicted']]
            multiple = fields[name].get('multiplicity', 'single') == 'multiple'
            status = ('missing' if not options else 'disputed' if any(o['contradicted'] for o in options.values())
                      else 'pending' if any(o['unresolved_counterevidence'] for o in options.values())
                      else 'supported' if supported and (multiple or len(options) == 1) else 'pending')
            row['field_status'][name] = dict(status=status,
                multiplicity='multiple' if multiple else 'single', alternatives=list(options.values()),
                supported_values=[o['value'] for o in supported])
            if options:
                # values retains a scalar representative for legacy consumers.
                # Report/coverage consumers use display_values and field_status;
                # unresolved single alternatives are never silently approved.
                row['values'][name] = (supported or list(options.values()))[0]['value']
                visible = supported if status == 'supported' else list(options.values())
                row['display_values'][name] = [o['value'] for o in visible]
        statuses = [f['status'] for f in row['field_status'].values()]
        row['missing_fields'] = [f for f, v in row['field_status'].items() if v['status'] == 'missing']
        row['status'] = ('invalid' if row.pop('invalid') else 'incomplete' if not row['identity_complete']
                         else 'disputed' if 'disputed' in statuses else 'incomplete' if 'missing' in statuses
                         else 'supported' if all(s == 'supported' for s in statuses) else 'pending')
        row['candidate_ids'] = list(dict.fromkeys(row['candidate_ids']))
        row['qualifications'] = list(dict.fromkeys(row['qualifications']))
    counts = Counter(row['status'] for row in groups.values())
    identified = sum(r['identity_complete'] and r['status'] != 'invalid' for r in groups.values())
    return dict(unit=section['unit'], required_fields=list(fields), identity_fields=identity_fields,
        count_basis=COUNT_BASIS, identified=identified, observed=identified, identity_pending=0,
        aggregation_version=AGGREGATION_VERSION, unassigned_count=len(unassigned),
        unassigned_observations=list(unassigned.values()),
        incomplete_count_basis='identified_records_missing_required_fields',
        **{s: counts[s] for s in ('supported', 'pending', 'disputed', 'incomplete', 'invalid')},
        claims_without_records=missing, records=list(groups.values()))
