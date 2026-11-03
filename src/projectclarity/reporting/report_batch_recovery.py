"""Retain usable batch findings without weakening report evidence checks.

Uncited summaries are kept in the task audit, not published as factual findings.
The application supplies its own batch-scope caveat. Missing facts are identified
for a separate bounded assessment; they are never silently marked irrelevant.
"""
import json


def inspect_batch(text, packet):
    """Validate the covered subset and identify work needing separate recovery.

    Args:
        text: Original model response, retained unchanged by the task receipt.
        packet: Exact batch facts and unchanged reporting requirements.
    Returns:
        Checked findings plus private recovery metadata. Uncited summary text is
        quarantined, not interpreted as true or automatically moved into gaps.
        Unknown or duplicate citation references still raise ValueError. Validly
        cited facts lose redundant exclusion entries in the checked result only.
        IDs are normalized before coverage accounting, so case/outer spaces do
        not manufacture omissions. Missing facts remain pending, never fabricated exclusions.
    """
    from projectclarity.reporting.report_generation import ReportResult, validate_report
    from projectclarity.reporting.report_reference_normalization import normalize_references
    result = normalize_references(ReportResult.model_validate_json(text).model_dump(),
        [f['id'] for f in packet['facts']], [r['id'] for r in packet['requirements']])
    uncited = [part['text'] for part in result['summary'] if not part['fact_ids']]
    result['summary'] = [part for part in result['summary'] if part['fact_ids']]
    used = {fid for part in result['summary'] for fid in part['fact_ids']}
    for entry in result['entries']:
        used.update(entry['label_fact_ids'])
        for field in entry['fields']:
            used.update(field['fact_ids'])
    accounted = used | {row['fact_id'] for row in result['exclusions']}
    missing = [fact['id'] for fact in packet['facts'] if fact['id'] not in accounted]
    covered = [fact for fact in packet['facts'] if fact['id'] in accounted]
    checked = validate_report(json.dumps(result), {**packet, 'facts': covered})
    if uncited:
        checked['gaps'].append('This is a partial evidence batch, not the complete research population. Uncited summary text was withheld; see the task audit.')
    checked['_recovery'] = {'missing_fact_ids': missing, 'withheld_uncited_summary': uncited}
    return checked
