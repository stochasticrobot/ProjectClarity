"""Keep result fields separate from the metadata identifying their supporting evidence.

A requested item might need name and affiliation fields. Its source URL, quotation
and passage ID are evidence metadata supplied by the application, not additional
identity fields the model must invent. These helpers remove explicitly reserved
evidence-field names from newly accepted plan shapes and record the original shape
for inspection. An item's own URL, such as document_url, remains a valid field.
The decision uses declared field names, not interpretation of claim prose.
"""

PROVENANCE_FIELDS = frozenset({
    'source_citation', 'source_citations', 'evidence_citation', 'evidence_citations',
    'evidence_quote', 'evidence_quotes', 'supporting_quote', 'supporting_quotes',
    'source_id', 'source_ids', 'passage_id', 'passage_ids', 'text_sha256',
    'source_url', 'source_urls', 'source_reference', 'source_references',
})


def item_fields(shape):
    """Return the required fields describing an item, excluding reserved evidence metadata.

    For example, name and affiliation can describe a researcher, while source_id and
    evidence_quote describe the support for that researcher. Removing the latter from
    identity prevents each citation from creating a different result.

    Args:
        shape: Serialized result shape, or None for an unshaped goal.
    Returns:
        Ordered, unique required names excluding explicitly reserved provenance
        fields. An empty list is not a usable enumeration identity. No mutation occurs.
    """
    return list(dict.fromkeys(name for name in (shape or {}).get('required_fields', [])
        if name.casefold() not in PROVENANCE_FIELDS))


def separate_plan_provenance(plan):
    """Remove redundant provenance requirements from a newly accepted plan.

    Args:
        plan: Mutable validated plan before extraction or human plan review.
    Returns:
        Audit receipts retaining the original fields and removed provenance names.
        Item fields and all research content are preserved. A provenance-only shape
        becomes null, explicitly leaving its item identity undefined rather than
        counting every claim as an item. Existing source evidence is never changed.
    """
    adjustments = []
    for goal in plan.goals:
        if goal.result_shape is None:
            continue
        original = goal.result_shape.model_dump()
        fields = item_fields(original)
        removed = [f for f in original['required_fields'] if f.casefold() in PROVENANCE_FIELDS]
        if not removed:
            continue
        adjustments.append({'goal_id': goal.goal_id, 'original_shape': original,
            'provenance_fields': removed, 'item_fields': fields,
            'status': 'provenance_separated' if fields else 'item_identity_undefined'})
        if fields:
            goal.result_shape.required_fields = fields
        else:
            goal.result_shape = None
    return adjustments
