"""Resolve exact report-row citations through their supplied provenance links."""


def normalize_result_references(answer, offered, ledgers):
    """Translate unambiguous row IDs in evidence citations, never population benchmarks.

    Args:
        answer: Parsed assessment; the original response remains unchanged.
        offered: Mapping of source-checked candidate IDs available for validation.
        ledgers: Exact report inventories supplied to this coverage assessment.
    Returns:
        Deep-copied assessment and per-criterion mapping receipts. Only row IDs
        resolving to one nonempty candidate set, wholly offered, are expanded.
        Existing candidate IDs take precedence; ambiguous or missing references
        remain unchanged for normal rejection. No identity inference or calls occur.
    """
    mappings = {}
    for ledger in ledgers.values():
        for row in ledger.get('items', []):
            if not row.get('result_id'):
                continue
            mappings.setdefault(row['result_id'], set()).add(tuple(sorted(set(row['candidate_ids']))))
    resolved = {key: next(iter(values)) for key, values in mappings.items()
        if key not in offered and len(values) == 1 and next(iter(values))
        and set(next(iter(values))) <= offered.keys()}
    normalized = answer.model_copy(deep=True)
    receipts = {}
    for assessment in normalized.assessments:
        used = {}
        for owner, attribute in [(assessment, 'evidence_candidate_ids'),
                *((item, 'candidate_ids') for item in assessment.items)]:
            expanded = []
            for cid in getattr(owner, attribute):
                if cid in resolved:
                    used[cid] = list(resolved[cid])
                expanded.extend(resolved.get(cid, (cid,)))
            setattr(owner, attribute, list(dict.fromkeys(expanded)))
        receipts[assessment.criterion_id] = used
    return normalized, receipts
