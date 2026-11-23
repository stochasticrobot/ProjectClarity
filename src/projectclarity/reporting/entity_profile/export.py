"""Export profile sections and uncertainty without flattening them into directory rows."""
import csv
from io import StringIO


def profile_csv(report):
    """Serialize the same findings, unknown sections and identity gaps shown on screen.

    Args:
        report: Validated profile artifact with original facts and evidence URLs.
    Returns:
        CSV text with subject, section, statement, status, citations and sources.
        All cells receive spreadsheet-formula protection. Identity uncertainty,
        gaps and unreviewed coverage are included; no calls or file writes occur.
    """
    from projectclarity.reporting.report_generation import csv_value
    stream = StringIO()
    writer = csv.writer(stream)
    writer.writerow(['Subject', 'Section', 'Finding', 'Status', 'Citations', 'Source URLs', 'Report coverage'])
    facts = {f['id']: f for f in report['facts']}
    subject = report['profile']['subject']
    coverage = 'Partial report' if report['coverage'].get('unreviewed_report') else 'Reviewed input accounted for'

    def row(section, text, status, ids):
        """Write one source-linked row with every untrusted cell protected.

        Args: section: Display heading; text: Finding or gap; status: Qualification;
            ids: Validated fact IDs, empty for unknown sections and general gaps.
        Returns: None; appends to the in-memory CSV stream without mutating report.
        """
        urls = sorted({e['source_url'] for fid in ids for e in facts.get(fid, {}).get('evidence', [])})
        writer.writerow([csv_value(v) for v in [subject, section, text, status, ', '.join(ids), '; '.join(urls), coverage]])

    row('Requested scope', report['profile']['subject_scope'] or 'No further identity qualifier supplied', 'User-requested scope, not a verified finding', [])
    for finding in report['summary']:
        row('Overview', finding['text'], finding['status'], finding['fact_ids'])
    for section in report['profile']['sections']:
        if not section['findings']:
            row(section['title'], 'Not established', 'unknown', [])
        for finding in section['findings']:
            row(section['title'], finding['text'], finding['status'], finding['fact_ids'])
    for item in report['profile']['identity_uncertainty']:
        row('Identity uncertainty', item['reason'], item['status'], [item['fact_id']])
    for gap in report['gaps']:
        row('Gap', gap, 'unresolved', [])
    return stream.getvalue()
