"""Combine structured findings without losing citations or unresolved conflicts.

Batch results can describe the same entity more than once. A bounded merge asks
the AI which findings really belong together. Local reference IDs keep requests
small; code expands them back to original fact citations. Summary merging uses
the same approach. Different names are not assumed to identify the same entity.
"""
from pydantic import BaseModel, ConfigDict
from projectclarity.reporting.report_generation import ReportEntry, CitedText


class MergedEntry(BaseModel):
    """One combined entry and the exact supplied findings it represents."""
    model_config = ConfigDict(extra='forbid', strict=True)
    input_ids: list[str]
    entry: ReportEntry


class MergedEntries(BaseModel):
    """Complete partition of the supplied findings, preserving separate identities."""
    model_config = ConfigDict(extra='forbid', strict=True)
    entries: list[MergedEntry]


class MergedSummary(BaseModel):
    """Cited narrative covering the supplied summary parts."""
    model_config = ConfigDict(extra='forbid', strict=True)
    summary: list[CitedText]


def merge_inputs(items, kind):
    """Replace potentially huge original citation lists with bounded local references.

    Args:
        items: Original-citation entries or summary paragraphs, retained by caller.
        kind: entries or summary, selecting which text structure to send.
    Returns: Local-ID/text records; original citations remain available for exact
        expansion after validation. No source evidence is invented or downloaded.
    """
    rows = []
    for index, item in enumerate(items):
        identifier = f'N{index+1}'
        if kind == 'entries':
            rows.append({'id': identifier, 'label': item['label'], 'fields': [
                {**{key: field[key] for key in ('requirement_id', 'status', 'value')}, 'has_evidence': bool(field['fact_ids'])}
                for field in item['fields']]})
        else:
            rows.append({'id': identifier, 'text': item['text']})
    return rows


def validate_merge(text, items, requirements, kind):
    """Expand local references only after checking full coverage and field continuity.

    Args:
        text: Raw model JSON.
        items: Exact pre-merge findings with original fact citations.
        requirements: Complete required report fields.
        kind: entries or summary.
    Returns: Expanded entries or summary paragraphs. Invalid IDs, omitted inputs,
        duplicate ownership, lost field evidence or erased known conflicts raise
        ValueError. Case/outer spaces normalize only against unique supplied IDs.
        This protects bookkeeping, not semantic identity judgments.
    """
    from projectclarity.reporting.report_reference_normalization import normalize_references
    original = {f'N{i+1}': item for i, item in enumerate(items)}
    if kind == 'summary':
        result = MergedSummary.model_validate(normalize_references(
            MergedSummary.model_validate_json(text).model_dump(), original, [r['id'] for r in requirements]))
        used = set()
        expanded = []
        for part in result.summary:
            if not part.text.strip() or not part.fact_ids or len(set(part.fact_ids)) != len(part.fact_ids) or not set(part.fact_ids) <= original.keys():
                raise ValueError('Invalid summary references.')
            used.update(part.fact_ids)
            expanded.append({'text': part.text, 'fact_ids': sorted({fact for identifier in part.fact_ids for fact in original[identifier]['fact_ids']})})
        if used != original.keys():
            raise ValueError('Summary merge omitted an input finding.')
        return expanded
    result = MergedEntries.model_validate(normalize_references(
        MergedEntries.model_validate_json(text).model_dump(), original, [r['id'] for r in requirements]))
    used, expanded = [], []
    required = {row['id'] for row in requirements}
    for merged in result.entries:
        identifiers = merged.input_ids
        if not identifiers or len(set(identifiers)) != len(identifiers) or not set(identifiers) <= original.keys():
            raise ValueError('Invalid merged finding ownership.')
        used.extend(identifiers)
        entry = merged.entry.model_dump()
        if not entry['label'].strip() or set(entry['label_fact_ids']) != set(identifiers) or len(entry['label_fact_ids']) != len(identifiers):
            raise ValueError('Merged label must retain every input identity.')
        if len(entry['fields']) != len(required) or {field['requirement_id'] for field in entry['fields']} != required:
            raise ValueError('Merged entry changed requirements.')
        entry['label_fact_ids'] = sorted({fact for identifier in identifiers for fact in original[identifier]['label_fact_ids']})
        for field in entry['fields']:
            previous = {identifier: next(f for f in original[identifier]['fields'] if f['requirement_id'] == field['requirement_id']) for identifier in identifiers}
            evidenced = {identifier for identifier, prior in previous.items() if prior['fact_ids']}
            if set(field['fact_ids']) != evidenced or len(field['fact_ids']) != len(evidenced):
                raise ValueError('Merged field dropped or invented input evidence.')
            if field['status'] == 'unknown' and field['value'].strip():
                raise ValueError('Unknown merged field has a value.')
            if field['value'].strip() and not evidenced:
                raise ValueError('Merged value has no evidence.')
            if field['status'] in ('supported', 'conflicting') and (not evidenced or not field['value'].strip()):
                raise ValueError('Merged field lacks supported content.')
            statuses = {prior['status'] for prior in previous.values()}
            values = {' '.join(prior['value'].casefold().split()) for prior in previous.values() if prior['status'] == 'supported'}
            if 'conflicting' in statuses and field['status'] != 'conflicting':
                raise ValueError('Merge erased an existing conflict.')
            combined_value = ' '.join(field['value'].casefold().split())
            if len(values) > 1 and any(value not in combined_value for value in values):
                raise ValueError('Merge lost differing values that may conflict.')
            if 'supported' not in statuses and field['status'] == 'supported':
                raise ValueError('Merge promoted incomplete evidence without support.')
            field['fact_ids'] = sorted({fact for prior in previous.values() for fact in prior['fact_ids']})
        expanded.append(entry)
    if len(set(used)) != len(used) or set(used) != original.keys():
        raise ValueError('Merge must account for each finding exactly once.')
    return expanded
