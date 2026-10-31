"""Associate partial report observations with an explicitly identified source subject.

A passage can describe one person's affiliation and research interests in separate
claims. Those claims may share the extractor's local subject ID while only the
affiliation claim supplies the report's complete identity. Reporting and mapping
checks use this module to associate the partial contribution with that identity.
The donor must be independently supported, belong to the same source version and
extraction batch, and agree with every supplied identity field. Multiple possible
identities prevent association. No names are guessed, no cross-passage aliases are
created, and no missing descriptive values or eligibility judgments are copied.
"""

from projectclarity.answer_schema.inventory import field_key


def subject_id(candidate):
    """Read an explicit local subject without guessing from a list of entities.

    Args:
        candidate: Serialized claim proposal with a relationship or entity references.
    Returns:
        Relationship subject, or the sole entity of a non-relational claim; None
        when no unambiguous subject was supplied. Entity IDs are batch-local only.
    """
    proposal = candidate['proposal']
    relationship = proposal.get('relationship')
    if relationship:
        return relationship.get('subject_id')
    entities = proposal.get('entity_ids', [])
    return entities[0] if len(entities) == 1 else None


def source_identity(candidate, values, candidates, goal, section):
    """Find one source-supported identity for a partial field contribution.

    Args:
        candidate: Anchored, supported claim whose partial record needs association.
        values: Original checked record fields; never changed or supplemented here.
        candidates: Source-verified candidates available to this accounting call.
        goal: Report goal defining allowed record fields.
        section: Report section declaring identity fields and their value kinds.
    Returns:
        Complete identity values and donor candidate IDs, or None when identity is
        missing, conflicting, unsupported or not explicitly linked. This only routes
        an observation; donors retain ownership of their fields and evidence. No
        dictionaries, source files or saved extraction claims are modified.
    """
    from projectclarity.research_results import record_check, structure_status
    identity = section['identity_fields']
    kinds = {f['name']: f['value_kind'] for f in section['fields']}
    subject = subject_id(candidate)
    scope = tuple(candidate.get(k) for k in ('source_id', 'text_sha256', 'batch_id'))
    supplied = {f: values[f] for f in identity if values.get(f, '').strip()}
    if (not subject or not all(scope) or not supplied
            or candidate.get('adjudication_status') != 'supported'
            or candidate.get('evidence_status') != 'anchored'
            or 'answer_evidence' not in candidate['proposal'].get('research_roles', [])):
        return None
    choices = {}
    for donor in candidates:
        if (tuple(donor.get(k) for k in ('source_id', 'text_sha256', 'batch_id')) != scope
                or subject_id(donor) != subject
                or donor.get('evidence_status') != 'anchored'
                or donor.get('adjudication_status') != 'supported'
                or 'answer_evidence' not in donor['proposal'].get('research_roles', [])
                or structure_status(donor['proposal']) not in {'structured', 'non_relational'}):
            continue
        for record in donor['proposal'].get('result_records', []):
            if record['goal_id'] != goal['goal_id']:
                continue
            checked = record_check(record, goal)
            fields = checked['values']
            if checked['errors'] or not all(fields.get(f, '').strip() for f in identity):
                continue
            if any(field_key(v, kinds[f]) != field_key(fields[f], kinds[f]) for f, v in supplied.items()):
                continue
            key = tuple((f, field_key(fields[f], kinds[f])) for f in identity)
            entry = choices.setdefault(key, {'values': {f: fields[f] for f in identity}, 'candidate_ids': []})
            entry['candidate_ids'].append(donor['candidate_id'])
    return next(iter(choices.values())) if len(choices) == 1 else None
