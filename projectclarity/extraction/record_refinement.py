"""Queue a focused reread when extraction leaves identifiable gaps in a passage.

A passage may contain several table rows or cards marked by the document parser.
After a completed extraction, these helpers record which such records were quoted
and which answer claims lack requested fields or a declared relationship. When
the conditions warrant it, they create follow-up extraction attempts for the
relevant record or the original passage, with quotations explaining the gap.

For example, a claim mentioning a person and employer but omitting the requested
relationship can prompt a reread of its source text. The original claims remain
unchanged. A follow-up cannot generate further follow-ups, and context-only
batches do not automatically trigger retries. Creating an attempt does not execute
it or guarantee that the missing information exists; the scheduler and extractor
handle execution within the existing run allowance.
"""
import hashlib

from projectclarity.extraction.extraction_contracts import ExtractionAttempt
from projectclarity.extraction.saved_source_passages import RECORD_START
from projectclarity.extraction.source_quote_matching import match_source_quote, QuoteMatchError
from projectclarity.research_results import result_defects


def refinement_context(attempt, extraction, goals, text):
    """Describe the missing structure that caused this passage to be scheduled for a reread.

    Include exact quotations from the original extraction and the detected issue codes,
    such as missing required result fields. Check each quotation against this child
    attempt's source slice; do not present the earlier model statement as a new fact.

    Args:
        attempt: Child extraction attempt pointing at its original batch.
        extraction: This run's serialized extraction ledger, never another run's data.
        goals: Serialized current goals defining optional result shapes.
        text: Hash-verified full source text; child offsets delimit its actual evidence.

    Returns:
        Application data with the parent ID, exact source quotations and detected
        structural issue codes. Prior assertions are not supplied as facts. Hints
        are capped at 6000 characters with an omitted count; the full child passage
        is still sent separately. No mutation, model calls or extra instructions.
    """
    parent = next((a for a in extraction.get('attempts', []) if a['batch_id'] == attempt.refinement_parent), {})
    issues, seen, used, omitted = [], set(), 0, 0
    passage = text[attempt.start:attempt.end]
    for claim in (parent.get('result') or {}).get('claims', []):
        defects = result_defects(claim, goals)
        if not defects:
            continue
        for evidence in claim['evidence']:
            if evidence['passage_id'] != parent.get('passage_id'):
                continue
            try:
                match = match_source_quote(passage, evidence['quote'])
            except QuoteMatchError:
                continue
            key = (match.quote, tuple(defects))
            if key in seen:
                continue
            seen.add(key)
            if used + len(match.quote) > 6000:
                omitted += 1
                continue
            issues.append({'source_quote': match.quote, 'detected_issues': defects})
            used += len(match.quote)
    return {'parent_batch_id': attempt.refinement_parent, 'issues': issues, 'omitted_issue_hints': omitted}


def record_followups(attempt, text, goals=()):
    """Create one-level reread attempts for records whose extraction has identifiable gaps.

    A parser-marked table row or card may be unquoted or represented by an answer claim
    missing a relationship or required result fields. Record those gaps and return
    focused child attempts where policy permits. The children keep exact source
    offsets and cannot recursively create further children.

    Args:
        attempt: Completed extraction receipt; its application-owned accounting is updated.
        text: Full hash-verified source text containing the attempt's original offsets.
        goals: Serialized goals with optional result shapes; empty preserves legacy
            record-attention behavior without guessing a result contract.

    Returns:
        Deterministic child ExtractionAttempts for unquoted or unstructured records when
        the batch contains multi-entity answer claims. Context/lead-only batches do not
        trigger extra calls. Children never spawn further children. A declared
        relational claim missing its relationship, or an answer missing required
        result fields, permits one follow-up even for plain text or a single record.
        Original claims are retained, not patched or replaced. Empty replies do not retry.
        Record attention is not semantic completeness, and unquoted records may be irrelevant.
    """
    if attempt.status != 'completed' or attempt.result is None:
        return []
    passage = text[attempt.start:attempt.end]
    markers = list(RECORD_START.finditer(passage))
    records = []
    for marker in markers:
        end = passage.find('\n\n', marker.end())
        end = len(passage) if end < 0 else end
        records.append((marker.group().strip()[8:-1], marker.start(), end))
    touched, structured, relational, defective_records = set(), set(), set(), set()
    has_grounded_defect = False
    answer_claims = [claim for claim in attempt.result.claims
                     if 'answer_evidence' in claim.research_roles
                     and ((claim.claim_kind != 'non_relational' and len(set(claim.entity_ids)) >= 2)
                          or result_defects(claim.model_dump(), goals))]
    for claim in attempt.result.claims:
        defects = result_defects(claim.model_dump(), goals)
        for evidence in claim.evidence:
            if evidence.passage_id != attempt.passage_id:
                continue
            try:
                span = match_source_quote(passage, evidence.quote)
            except QuoteMatchError:
                continue
            if defects:
                has_grounded_defect = True
            for record_id, start, end in records:
                if start <= span.start and span.end <= end:
                    touched.add(record_id)
                    if defects:
                        defective_records.add(record_id)
                    if claim.relationship is not None:
                        structured.add(record_id)
                    elif claim in answer_claims:
                        relational.add(record_id)
    attempt.unquoted_record_ids = [key for key, _, _ in records if key not in touched]
    attempt.unstructured_record_ids = sorted((relational - structured) | defective_records)
    if attempt.refinement_parent or not answer_claims:
        return []
    needs = set(attempt.unquoted_record_ids) | set(attempt.unstructured_record_ids)
    if len(records) < 2:
        # New explicit defects may use one exact-passage follow-up. A legacy
        # one-record reply with unspecified classification retains its old policy.
        if not has_grounded_defect:
            return []
        records = [('passage', 0, len(passage))]
        needs = {'passage'}
    elif has_grounded_defect and not defective_records:
        # A located quotation outside parser-marked table records has no known row.
        # Use one passage follow-up instead of guessing which row it describes.
        records = [('passage', 0, len(passage))]
        needs = {'passage'}
    children = []
    for key, start, end in records:
        if key not in needs:
            continue
        absolute_start, absolute_end = attempt.start + start, attempt.start + end
        token = hashlib.sha256(f'{attempt.batch_id}:{absolute_start}:{absolute_end}'.encode()).hexdigest()[:20]
        passage_id = 'R' + token
        children.append(ExtractionAttempt(batch_id=f'{attempt.source_id}_{passage_id}',
            source_id=attempt.source_id, text_sha256=attempt.text_sha256,
            passage_id=passage_id, start=absolute_start, end=absolute_end,
            refinement_parent=attempt.batch_id))
    attempt.record_followup_ids = [child.batch_id for child in children]
    return children
