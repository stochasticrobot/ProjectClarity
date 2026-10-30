"""Format the designed report as Markdown using the existing result inventory.

The provisional-answer renderer supplies checked observations and counts. This
module displays every report row, its field statuses and evidence references,
then lists unassigned observations separately. For example, a supported name with
an unknown required affiliation is not presented as a complete supported person.
Narrative sections list supported statements without synthesizing new prose.
Rendering makes no model requests, changes no evidence and saves no files."""


def cell(value):
    """Keep a report value inside one Markdown table cell.

    Args:
        value: Proposed display value; missing values are labelled unknown.
    Returns:
        Single-line text with pipes escaped. No evidence or values are inferred.
    """
    return str(value if value is not None else 'Unknown').replace('|', '\\|').replace('\n', ' ')


def render_report(answer):
    """Present answer-shaped entries before the underlying claim catalogue.

    Args:
        answer: Fresh provisional answer with answer_schema, progress and findings.
    Returns:
        Markdown lines; empty for legacy runs. Lists every entry with field status
        and candidate references. Narrative sections retain supported claim text,
        not invented synthesis or automatic completeness. No state changes occur.
    """
    schema = answer.get('answer_schema')
    if not schema:
        return []
    lines = ['', '## ' + schema['title'], '', schema['scope'],
             '', 'Working report: source support is not a guarantee of completeness or currentness.']
    if schema.get('as_of'):
        lines.append('Requested as of: ' + schema['as_of'])
    progress = {g['goal_id']: g for g in answer.get('progress', {}).get('goals', [])}
    for section in schema['sections']:
        lines.extend(['', '### ' + section['title'], ''])
        if section['format'] == 'narrative':
            findings = [r for r in answer.get('findings', []) if section['goal_id'] in r['goal_ids']]
            lines.extend('- ' + r['statement'] + ' [' + ', '.join(c['candidate_id'] for c in r['citations']) + ']' for r in findings)
            if not findings:
                lines.append('No supported findings for this section yet.')
            lines.append('Section completeness requires its evidence checks; claim counts do not establish it.')
            continue
        fields = [f['name'] for f in section['fields']]
        lines += ['| ' + ' | '.join([*fields, 'Status', 'Evidence']) + ' |',
                  '| ' + ' | '.join(['---'] * (len(fields) + 2)) + ' |']
        inventory = progress.get(section['goal_id'], {}).get('results') or {}
        for row in inventory.get('records', []):
            values = [cell('; '.join(row.get('display_values', {}).get(f, [])) or row['values'].get(f)) +
                      (' (' + row['field_status'][f]['status'] + ')' if row.get('field_status', {}).get(f, {}).get('status') not in {None, 'supported'} else '')
                      for f in fields]
            lines.append('| ' + ' | '.join([*values, row['status'], ', '.join(row['candidate_ids'])]) + ' |')
        if not inventory.get('records'):
            lines.append('No report entries yet.')
        unassigned = inventory.get('unassigned_observations', [])
        if unassigned:
            lines += ['', '#### Unassigned evidence', '',
                      'These observations lack a complete identity and are not counted as report entries.', '']
            for observation in unassigned:
                values = '; '.join(f + ': ' + cell(v) for f, v in observation['values'].items())
                lines.append('- ' + values + ' [' + ', '.join(observation['candidate_ids']) + ']')
    citations = {c['candidate_id']: c for category in ('findings', 'counterevidence', 'disputed', 'leads')
                 for finding in answer.get(category, []) for c in finding['citations']}
    if citations:
        lines += ['', '### Report evidence', '']
        for cid, citation in citations.items():
            lines.append('- ' + cid + ': ' + citation['location'])
    return lines
