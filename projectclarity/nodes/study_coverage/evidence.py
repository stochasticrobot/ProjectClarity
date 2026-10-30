"""Prepare short source excerpts and check quotations used by preliminary estimates.

A preliminary study samples pages before the main research loop. These helpers
select bounded excerpts from the beginning, middle and end of the saved text and
check that estimate references point to the supplied source material. Omitted text
is reported rather than silently treated as reviewed. Matching a quotation supports
traceability; it does not make the estimate an exact total or a reviewed finding.
"""
from projectclarity.extraction.saved_source_passages import EvidenceInputError, read_verified_text


def collect_probe_text(session, output_root):
    """Read checked excerpts from the pages selected for the preliminary source study.

    Offer up to 6,000 characters per page, sampling the beginning, middle and end of
    long text. Keep original character offsets and an explicit truncation label so
    the model is not presented with a short excerpt as if it were the complete page.

    Args:
        session: Run whose study identifies at most four acquired/reused pages.
        output_root: Run root containing immutable acquisition receipts and text files.
    Returns:
        Page payloads and failures. Each page offers at most 6,000 characters with
        original offsets, never search snippets. Source hash failures exclude that
        page. Full text remains on disk for ordinary extraction after the study.
    """
    documents = {d.source_id: d for d in session.documents}
    pages, failures = [], []
    for source_id in session.coverage_study.get('source_ids', []):
        document = documents.get(source_id)
        if document is None or document.status != 'retrieved':
            failures.append({'source_id': source_id, 'error_code': document.error_code if document else 'not_retrieved'})
            continue
        try:
            text = read_verified_text(output_root / session.session_id, document)
        except EvidenceInputError:
            failures.append({'source_id': source_id, 'error_code': 'source_integrity_failed'})
            continue
        spans = [(0, len(text))] if len(text) <= 6000 else [(0, 2000), ((len(text) - 2000) // 2, 2000), (len(text) - 2000, 2000)]
        pages.append({'source_id': source_id, 'text_sha256': document.text_sha256,
            'url_or_path': document.original_location,
            'total_characters': len(text), 'truncated': len(text) > 6000,
            'excerpts': [{'start': start, 'text': text[start:start + size]} for start, size in spans]})
    return pages, failures


def validate_estimates(session, answer, pages, output_root, *, fallback_allowed):
    """Check source references and permitted estimate types in a preliminary-study reply.

    An evidence-informed estimate must quote text actually offered to the model and
    still present exactly once in the unchanged saved source. A model-only estimate
    requires explicit fallback permission. These checks validate traceability and
    format, not the model's numerical judgment or research completion.

    Args:
        session: Run defining permitted goal IDs and current document receipts.
        answer: Parsed preliminary assessment; the raw proposal is retained separately.
        pages: Exact excerpts actually offered to the model, including their source hashes.
        output_root: Run root used to recheck full saved source text after model execution.
        fallback_allowed: Whether an unavailable search and the explicit fallback flag
            permit unsupported model priors in this particular assessment.
    Returns:
        Accepted estimates and individual rejection receipts. Evidence-informed ranges
        require exact, uniquely locatable quotes in both offered excerpts and unchanged
        full sources. Unknown and prior estimates cannot carry source evidence. Counts
        and semantic applicability remain model judgments; no completion target changes.
        Numeric estimates are rejected when the task profile declares them inapplicable.
    """
    goals = {g.goal_id for g in session.plan.goals}
    offered = {p['source_id']: p for p in pages}
    documents = {d.source_id: d for d in session.documents}
    accepted, rejected, texts = [], [], {}
    for index, estimate in enumerate(answer.estimates, 1):
        error, references = None, []
        if not answer.readiness.numeric_estimate_applicable:
            error = 'numeric_estimate_not_applicable'
        elif set(estimate.goal_ids) - goals:
            error = 'unknown_goal'
        elif estimate.basis == 'unknown' and (estimate.range is not None or estimate.evidence):
            error = 'invalid_unknown_estimate'
        elif estimate.basis == 'provisional_prior' and (not fallback_allowed or estimate.evidence):
            error = 'fallback_not_allowed_or_has_evidence'
        elif estimate.basis == 'evidence_informed':
            if not estimate.evidence:
                error = 'missing_source_evidence'
            for evidence in estimate.evidence:
                page, document = offered.get(evidence.source_id), documents.get(evidence.source_id)
                if not page or not document or page['text_sha256'] != document.text_sha256:
                    error = 'unknown_or_changed_source'
                    break
                try:
                    if evidence.source_id not in texts:
                        texts[evidence.source_id] = read_verified_text(output_root / session.session_id, document)
                    text = texts[evidence.source_id]
                except EvidenceInputError:
                    error = 'source_integrity_failed'
                    break
                if not any(evidence.quote in e['text'] for e in page['excerpts']) or text.count(evidence.quote) != 1:
                    error = 'quote_not_offered_or_not_unique'
                    break
                start = text.index(evidence.quote)
                references.append({**evidence.model_dump(mode='json'), 'text_sha256': document.text_sha256,
                    'start': start, 'end': start + len(evidence.quote)})
        if error:
            rejected.append({'estimate_index': index, 'error_code': error})
        else:
            accepted.append({**estimate.model_dump(mode='json'), 'estimate_id': f'CS_E{index}',
                'evidence': references, 'review_status': 'preliminary_not_claim_reviewed'})
    return accepted, rejected
