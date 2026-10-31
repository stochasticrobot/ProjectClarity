"""Keep report inclusion requirements separate from factual claim support.

A source can establish that a person was affiliated with a laboratory without
establishing a current affiliation. Reviewers therefore assess the report's scope
and requested date separately for each proposed record. Section-wide evidence
requirements remain whole-answer completion obligations, not checks each person
must satisfy alone. Historical version-one receipts retain their original policy. This module supplies stable requirement IDs, validates their source-bound
receipts, and combines decisions only for observations already joined by the
report's identity rules. It does not infer eligibility from reviewer prose, a URL,
an acquisition timestamp, a filled field, or a supported factual verdict.
"""
import hashlib
import json

from projectclarity.source_context import source_context
from projectclarity.extraction.source_quote_matching import match_source_quote, QuoteMatchError

LEGACY_POLICY = 'fact-and-qualification-v1'
POLICY = 'record-scope-and-currentness-v2'


def record_fingerprint(record):
    """Bind an inclusion decision to the exact submitted record, not its index alone.

    Args:
        record: Serialized goal and field values from a candidate's result_records.
    Returns:
        Stable SHA-256 digest. Reordered or changed record contents require a new
        judgment; no field value or identity is inferred and no state is changed.
    """
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


def requirement_catalog(schema, policy=POLICY):
    """Bind inclusion checks to the exact saved report specification.

    Args:
        schema: Serialized answer specification, or None for an undesigned run.
        policy: Saved inclusion policy. Version one retains its original section-level
            evidence checks for historical receipts; version two limits entry checks
            to scope and currentness. Section evidence remains a completion obligation.
    Returns:
        Goal-keyed entry checks. Section-wide reconciliation is never assigned to an
        individual entry under version two. Historical version-one IDs are retained.
        IDs change when the report scope, date, fields or requirements change, so
        an assessment of an older contract cannot silently qualify a new report.
        No model instructions are synthesized and no I/O occurs.
    """
    if not schema:
        return {}
    result = {}
    for section in schema['sections']:
        if not section['fields']:
            continue
        context = dict(scope=schema['scope'], as_of=schema.get('as_of'), section=section)
        requirements = [dict(kind='scope', scope=schema['scope'], question=section['question'])]
        if schema.get('as_of'):
            requirements.append(dict(kind='currentness', as_of=schema['as_of']))
        if policy == LEGACY_POLICY:
            requirements.extend(dict(kind='evidence', description=text) for text in section['evidence_requirements'])
        else:
            context['qualification_policy'] = policy
        rows = []
        for index, requirement in enumerate(requirements):
            token = hashlib.sha256(json.dumps([context, index, requirement], sort_keys=True).encode()).hexdigest()[:16]
            rows.append(dict(requirement_id=f'{section["goal_id"]}_{token}', **requirement))
        result[section['goal_id']] = rows
    return result


def qualification_work(candidate, catalog):
    """Count requested record-level judgments when packing a review batch.

    Args:
        candidate: Typed candidate proposal whose result records will be offered.
        catalog: Goal-keyed inclusion requirements for the current answer.
    Returns:
        Number of record/requirement decisions requested for answer evidence, or
        zero for context-only claims. This is an output-size bound, not token usage.
    """
    return (sum(len(catalog.get(r.goal_id, [])) for r in candidate.proposal.result_records)
            if 'answer_evidence' in candidate.proposal.research_roles else 0)


def validate_qualifications(session, candidate, item, attempt, text):
    """Validate inclusion decisions without overwriting the factual review outcome.

    Args:
        session: Typed run providing the report specification and assigned source context.
        candidate: Typed candidate owning the indexed result records.
        item: Parsed claim review, including separately supplied eligibility decisions.
        attempt: Original extraction passage defining the permitted quote window.
        text: Verified full source text; offsets in receipts remain document-relative.
    Returns:
        A receipt for every expected record/requirement pair. Missing, duplicated,
        misattributed or ungrounded decisions become unknown with an error code.
        Exact quotes and cited context values are retained; no source is fetched and
        neither the session nor the factual verdict is changed. Unknown extra IDs
        cannot displace a correctly attributed expected decision.
    """
    catalog = requirement_catalog(session.answer_schema, session.workflow.get('claim_support_policy', POLICY))
    context = source_context(session, candidate.source_id)
    decisions = [q.model_dump(mode='json') for q in item.eligibility_assessments]
    result = []
    if 'answer_evidence' not in candidate.proposal.research_roles:
        return result
    for index, record in enumerate(candidate.proposal.result_records):
        for requirement in catalog.get(record.goal_id, []):
            matches = [q for q in decisions if (q['record_index'], q['goal_id'], q['requirement_id'])
                       == (index, record.goal_id, requirement['requirement_id'])]
            row = dict(record_index=index, goal_id=record.goal_id, requirement_id=requirement['requirement_id'],
                record_sha256=record_fingerprint(record.model_dump(mode='json')),
                verdict='unknown', rationale='No valid inclusion assessment was supplied.', evidence=[], context={},
                error_code='missing_qualification' if not matches else 'duplicate_qualification')
            if len(matches) == 1:
                decision = matches[0]
                error, anchors, cited = None, [], {}
                for quote in decision['evidence_quotes']:
                    try:
                        match = match_source_quote(text[attempt.start:attempt.end], quote)
                        anchors.append(dict(start=attempt.start + match.start, end=attempt.start + match.end,
                                            quote=match.quote))
                    except QuoteMatchError as exc:
                        error = 'qualification_' + str(exc)
                for key in decision['context_keys']:
                    value = context.get(key)
                    if not value:
                        error = 'qualification_context_missing'
                    else:
                        cited[key] = value
                if decision['verdict'] != 'unknown' and not anchors and not (set(cited) - {'acquired_at'}):
                    error = 'qualification_missing_evidence'
                row.update(verdict='unknown' if error else decision['verdict'], rationale=decision['rationale'],
                           evidence=anchors, context=cited, error_code=error)
            result.append(row)
    return result


def qualify_record(row, contributions, section, data, candidates):
    """Attach inclusion judgments to one report entry and gate its supported status.

    Args:
        row: Mutable assembled report entry, with per-field factual support already set.
        contributions: Candidate-ID/record-index pairs actually assigned to this entry.
        section: Report section defining the entry's evidence requirements.
        data: Serialized session containing the review policy, schema and receipts.
        candidates: Source-verified candidate dictionaries available to this inventory.
    Returns:
        None. Adds separate field_support_status and qualification details to row.
        New-policy entries with unresolved/conflicting/unmet requirements remain
        pending even when every field is supported. Unknown local evidence does not
        erase another contribution's affirmative evidence; met/not_met disagreement
        remains conflicting. Legacy rows keep historical field counts and receive
        an explicit unassessed-legacy label, never fabricated inclusion approval.
    """
    row['field_support_status'] = row['status']
    policy = data.get('workflow', {}).get('claim_support_policy')
    if policy not in {POLICY, LEGACY_POLICY}:
        row['qualification_status'] = 'not_assessed_legacy'
        return
    by_id = {c['candidate_id']: c for c in candidates}
    reviews = data.get('claim_reviews', {})
    details = []
    for requirement in requirement_catalog(data.get('answer_schema'), policy).get(section['goal_id'], []):
        evidence = []
        for cid, index in contributions:
            candidate = by_id[cid]
            review = reviews.get(cid, {})
            if (candidate.get('adjudication_status') != 'supported' or review.get('error_code')
                    or review.get('text_sha256') != candidate.get('text_sha256')):
                continue
            for decision in review.get('qualification_reviews', []):
                if (decision['record_index'], decision['goal_id'], decision['requirement_id']) == (
                        index, section['goal_id'], requirement['requirement_id']) and decision.get('record_sha256') == record_fingerprint(
                            candidate['proposal']['result_records'][index]):
                    checked = dict(decision)
                    if checked['verdict'] != 'unknown' and not checked.get('evidence') and not (
                            set(checked.get('context', {})) - {'acquired_at'}):
                        checked.update(verdict='unknown', error_code='qualification_missing_evidence')
                    evidence.append(dict(candidate_id=cid, source_id=candidate.get('source_id'), **checked))
        verdicts = {e['verdict'] for e in evidence if not e.get('error_code')}
        status = ('conflicting' if {'met', 'not_met'} <= verdicts else 'not_met' if 'not_met' in verdicts
                  else 'met' if 'met' in verdicts else 'unknown')
        details.append(dict(**requirement, status=status, assessments=evidence))
    row['qualification_requirements'] = details
    statuses = {d['status'] for d in details}
    row['qualification_status'] = ('conflicting' if 'conflicting' in statuses else 'not_met' if 'not_met' in statuses
        else 'unknown' if not details or 'unknown' in statuses else 'met')
    if row['status'] == 'supported' and row['qualification_status'] != 'met':
        row['status'] = 'pending'
