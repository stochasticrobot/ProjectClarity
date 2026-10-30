"""Compare extracted member-of-Parliament and riding pairs with a frozen page's answer key.

A riding is the electoral district represented by a member. The scorer checks
structured person–riding relationships and their source references, not just the
number of names mentioned. It reports missing and unexpected pairs and keeps
strict scores separate from explicitly permitted source-equivalence comparisons.
This benchmark-specific code never supplies expected pairs to the extractor or
asks a model to grade itself. A page match is not proof of present-day membership.
"""
import hashlib
import json
import re
from pathlib import Path

from evaluation.membership_result_scoring import normalize_name, relation_metrics
from projectclarity.extraction.extraction_contracts import ExtractionAttempt
from projectclarity.extraction.local_identifier_recovery import recover_local_identifiers
from projectclarity.nodes.validate_evidence.node import validate_batch


def person_name(value):
    """Normalize explicit display-order/title variants without guessing identity.

    Args:
        value: Person label emitted by extraction or present in the page reference.

    Returns:
        NFC/case/whitespace-normalized name, with a single Last, First order reversed
        and conventional parliamentary display titles removed. Accents, initials,
        nicknames and differing words are not reconciled.
    """
    value = normalize_name(value)
    if value.count(',') == 1:
        last, first = value.split(',', 1)
        value = first.strip() + ' ' + last.strip()
    return re.sub(r'^(?:(?:the )?(?:right honourable|honourable)|right hon\.|rt\. hon\.|hon\.)\s+', '', value)


def score_page(directory: Path, reference_path: Path, *, recover_ids=False, source_equivalences=False):
    """Measure source-local structured pairs and retain errors, omissions and scope limits.

    Args:
        directory: Saved production extraction run directory, read without mutation.
        reference_path: Independent expected JSON for exactly this frozen raw page.
        recover_ids: Replay only deterministic identifier-format recovery on failed
            completed replies; original receipts remain unchanged and the policy is labelled.
        source_equivalences: Separately label an optional comparison accepting exact
            riding-plus-province aliases explicitly supplied by this page's reference,
            and excluding explicitly historical/uncertain former-member context. Default
            False preserves the strict comparator; neither mode changes extracted data.

    Returns:
        JSON-compatible scores for all proposed and anchored pairs, plus missing,
        unexpected, historical-context and unscored claims. This is relationship
        extraction accuracy, not independent factual verification or review approval.
        Source/hash mismatches and invalid inputs raise rather than scoring another page.
    """
    reference = json.loads(reference_path.read_text())
    session = json.loads((directory / 'session.json').read_text())
    record = json.loads((directory / 'extraction.json').read_text())
    if len(session['documents']) != 1:
        raise ValueError('A page benchmark requires exactly one saved source.')
    document = session['documents'][0]
    raw = (directory / document['raw_path']).read_bytes()
    text = (directory / document['text_path']).read_text()
    if hashlib.sha256(raw).hexdigest() != reference['source_sha256']:
        raise ValueError('The saved HTML does not match this frozen reference.')
    if hashlib.sha256(text.encode()).hexdigest() != document['text_sha256']:
        raise ValueError('The saved text no longer matches its source receipt.')
    expected = {(person_name(r['mp']), normalize_name(r['riding'])) for r in reference['expected_pairs']}
    historical = {(person_name(r['mp']), normalize_name(r['riding'])) for r in reference['excluded_historical_pairs']}
    historical_people = {mp for mp, _ in historical} - {mp for mp, _ in expected}
    current_people = {mp for mp, _ in expected}
    riding_aliases = {normalize_name(f"{r['riding']} ({r['province']})"): normalize_name(r['riding'])
                      for r in reference['expected_pairs'] if r.get('province')}
    applied_aliases = []
    proposed, anchored = set(), set()
    contextual_history, unscored, invalid, repairs = [], [], [], []
    failures, unfinished = [], []
    for raw_attempt in record['attempts']:
        attempt = ExtractionAttempt.model_validate(raw_attempt)
        if attempt.status in ('not_attempted', 'started'):
            unfinished.append(attempt.batch_id)
            continue
        if attempt.result is None and recover_ids and attempt.error_code == 'invalid_extraction_schema':
            trace = json.loads((directory / attempt.trace_path).read_text())
            if trace['stop_reason'] == 'end_turn' and not trace['response_text_truncated']:
                try:
                    attempt.result, aliases = recover_local_identifiers(trace['response_text'])
                    repairs.append({'batch_id': attempt.batch_id, 'aliases': aliases})
                except (ValueError, KeyError, TypeError):
                    pass
        if attempt.result is None:
            failures.append({'batch_id': attempt.batch_id, 'error_code': attempt.error_code})
            continue
        entities = {e.entity_id: e for e in attempt.result.entities}
        candidates = validate_batch(attempt, text, {g['goal_id'] for g in session['plan']['goals']})
        for candidate in candidates:
            claim = candidate.proposal
            rel = claim.relationship
            if rel is None or rel.subject_id not in entities or rel.object_id not in entities:
                unscored.append(candidate.candidate_id)
                continue
            subject, obj = entities[rel.subject_id], entities[rel.object_id]
            subject_kind = normalize_name(subject.entity_type)
            kind = normalize_name(obj.entity_type)
            if (not any(word in subject_kind for word in ('person', 'member', 'politician', ' mp'))
                    and subject_kind != 'mp') or not any(word in kind for word in ('riding', 'constituency', 'electoral district')):
                unscored.append(candidate.candidate_id)
                continue
            pair = person_name(subject.name), normalize_name(obj.name)
            if source_equivalences and pair[1] in riding_aliases:
                applied_aliases.append({'candidate_id': candidate.candidate_id,
                    'source_label': obj.name, 'canonical_riding': riding_aliases[pair[1]],
                    'basis': 'Exact riding/province values in this page-specific reference.'})
                pair = pair[0], riding_aliases[pair[1]]
            # A correctly qualified former-member relationship is context, not a
            # claim of current service. Unqualified former-member pairs remain errors.
            wording = ' '.join([claim.statement, rel.predicate, *claim.qualifications]).lower()
            historical_context = pair[0] in historical_people and any(
                word in wording for word in ('until ', 'former ', 'resigned', 'step down'))
            if source_equivalences:
                historical_context |= (pair[0] in historical_people and 'does not state' in wording
                                       and ('current' in wording or 'status as of' in wording))
                historical_context |= (pair[0] not in current_people and 'former member' in wording
                                       and 'context' in claim.research_roles)
            if historical_context:
                contextual_history.append({'candidate_id': candidate.candidate_id, 'pair': pair})
                continue
            proposed.add(pair)
            if candidate.evidence_status == 'anchored':
                anchored.add(pair)
            else:
                invalid.append({'candidate_id': candidate.candidate_id, 'pair': pair,
                                'validation_errors': candidate.validation_errors})
    return {'schema_version': 1, 'policy': 'page-pairs-v2-source-equivalences' if source_equivalences else 'page-pairs-v1',
            'reference_sha256': hashlib.sha256(reference_path.read_bytes()).hexdigest(),
            'source_sha256': reference['source_sha256'], 'run_id': session['session_id'],
            'identifier_recovery_replay': recover_ids, 'new_model_calls': 0,
            'source_equivalences': source_equivalences, 'applied_source_aliases': applied_aliases,
            'normalization': 'NFC/case/whitespace; explicit Last, First reversal and conventional titles. No nickname, accent-stripping or riding-renaming equivalences. Optional province qualifiers are listed separately.',
            'proposed_pairs': relation_metrics(expected, proposed),
            'anchored_pairs': relation_metrics(expected, anchored),
            'qualified_historical_context': contextual_history, 'unscored_claim_ids': unscored,
            'invalid_evidence': invalid, 'identifier_repairs': repairs,
            'failed_batches': failures, 'unfinished_batches': unfinished,
            'scope': 'Source-local pair accuracy, not independent currency or semantic quote entailment. Empty/missing relationships are not reconstructed from prose.'}
