"""Compare a saved observation graph against an independent, finite fixture answer key.

Only documented, versioned representation equivalences are accepted. Older
experiments without a policy retain their original strict comparison rules.
No model grades its own work. Evidence checks require the fixture's supporting
sentence, not merely a quotation somewhere on the page. This is a strict fixture
contract, not a general semantic-equivalence or factual-truth evaluator.
"""
import hashlib
import json

from projectclarity.extraction.saved_source_passages import read_verified_text
from projectclarity.knowledge.graph_store import read_knowledge
from projectclarity.research_schemas import ResearchSession
from evaluation.graph_comparison_policy import ComparisonPolicy, comparison_key


def normalized(value):
    """Normalize text layout for comparison without inventing aliases or changing direction.

    Args:
        value: A scalar label or value from the expected or observed graph.

    Returns:
        Case-folded, whitespace-collapsed text; numeric 2024 and textual '2024' compare
        equally. None becomes an empty label, not a guessed role.
    """
    return '' if value is None else ' '.join(str(value).casefold().split())


def canonical_predicate(value, reference):
    """Apply the published finite alias list so spelling differences do not hide graph errors.

    Args:
        value: Observed or expected predicate label.
        reference: Frozen answer key containing the allowed predicate aliases.

    Returns:
        A normalized canonical label; unknown predicates remain distinct and unexpected.
    """
    candidate = normalized(value).replace(' ', '_')
    for name, aliases in reference['predicate_aliases'].items():
        if candidate in {normalized(label).replace(' ', '_') for label in [name, *aliases]}:
            return normalized(name)
    return candidate


def record_key(record, kind, reference, policy=None):
    """Convert one labelled record into a direction-sensitive comparison key.

    Args:
        record: Expected or observed record using names rather than database IDs.
        kind: nodes, relationships or attributes.
        reference: Frozen answer key, including the legacy finite predicate aliases.
        policy: Optional validated version-2 equivalences; None preserves legacy strict rules.

    Returns:
        A hashable tuple that retains type, endpoint order, relationship role and attribute
        value under the selected policy. Node UUIDs and evidence offsets are deliberately
        not identity criteria; raw observations are never changed.
    """
    if policy is not None:
        return comparison_key(record, kind, policy)
    if kind == 'nodes':
        return normalized(record['name']), normalized(record['kind'])
    predicate = canonical_predicate(record['predicate'], reference)
    if kind == 'relationships':
        return normalized(record['subject']), predicate, normalized(record['object']), normalized(record.get('role'))
    return normalized(record['subject']), predicate, normalized(record['value'])


def describe(record, kind):
    """Render a compact record label so differences are readable without opening raw logs.

    Args:
        record: Labelled node, relationship or attribute dictionary.
        kind: The record's comparison category.

    Returns:
        A one-line human-readable name/type, directed relationship or attribute assignment.
    """
    if kind == 'nodes':
        return f"{record['name']} ({record['kind']})"
    if kind == 'attributes':
        return f"{record['subject']} — {record['predicate']} = {record['value']}"
    role = f" [role: {record['role']}]" if record.get('role') else ''
    return f"{record['subject']} — {record['predicate']} → {record['object']}{role}"


def evidence_valid(record, text, version_ids):
    """Check a stored quotation against the verified fixture source rather than trusting a tag.

    Args:
        record: Stored mention or assertion with source_version and evidence fields.
        text: Hash-verified normalized source text supplied to the production pipeline.
        version_ids: Set of source-version IDs matching this fixture's location and hash.

    Returns:
        True only for a nonempty exact source slice with valid integer offsets and an
        allowed source version. Malformed evidence returns False rather than counting.
    """
    evidence = record.get('evidence', {})
    start, end = evidence.get('start'), evidence.get('end')
    return (record.get('source_version') in version_ids and type(start) is int and type(end) is int
        and 0 <= start < end <= len(text) and text[start:end] == evidence.get('quote'))


def compare_graph(reference, graph, text, version_ids, policy=None):
    """Compare actual SQLite records, retaining every missing, unexpected or unsupported item.

    Args:
        reference: Independent expected nodes, directed relationships, attributes and quotes.
        graph: Snapshot read from the production knowledge database.
        text: Verified fixture text used for anchoring observed evidence.
        version_ids: Source-version IDs proven to belong to that fixture.
        policy: Optional versioned equivalence rules; None retains the legacy comparator.

    Returns:
        Per-category expected/matched counts, row results and unexpected records, plus
        evidence errors. Extra or reversed edges cannot be hidden by matching total counts.
    """
    observed = {'nodes': [], 'relationships': [], 'attributes': []}
    errors = []
    nodes = {node['id']: node for node in graph['nodes']}
    anchored = set()
    for mention in graph['mentions']:
        node = nodes.get(mention.get('node_id'))
        if (node and evidence_valid(mention, text, version_ids)
                and normalized(node['name']) in normalized(mention['evidence']['quote'])):
            anchored.add(node['id'])
        else:
            errors.append('Invalid node evidence: ' + str(mention.get('id')))
    for node in nodes.values():
        observed['nodes'].append(node)
        if node['id'] not in anchored:
            errors.append('Node has no valid source mention: ' + node['name'])
    for assertion in graph['assertions']:
        subject = nodes.get(assertion.get('subject'))
        target = nodes.get(assertion.get('object')) if assertion.get('object') is not None else None
        if (not subject or subject['id'] not in anchored
                or (assertion.get('object') is not None and (not target or target['id'] not in anchored))
                or not evidence_valid(assertion, text, version_ids)):
            errors.append('Invalid assertion endpoints/evidence: ' + str(assertion.get('id')))
            continue
        kind = 'relationships' if target else 'attributes'
        record = {**assertion, 'subject': subject['name'], 'object': target['name'] if target else None}
        if (target and assertion.get('value') is not None) or (not target and assertion.get('value') is None):
            errors.append('Assertion must be exactly one edge or attribute: ' + str(assertion.get('id')))
            continue
        observed[kind].append(record)
    result = {'evidence_errors': errors, 'categories': {}}
    for kind, records in observed.items():
        expected = {record_key(row, kind, reference, policy): row for row in reference[kind]}
        actual = {}
        for row in records:
            actual.setdefault(record_key(row, kind, reference, policy), []).append(row)
        rows = []
        for key, expected_row in expected.items():
            matches = actual.get(key, [])
            supported_matches = [row for row in matches if (row['id'] in anchored if kind == 'nodes' else
                normalized(expected_row['quote']).rstrip('.') in normalized(row['evidence']['quote']))]
            supported = bool(supported_matches)
            status = 'matched' if supported else 'unsupported' if matches else 'missing'
            rows.append({'expected': describe(expected_row, kind), 'status': status,
                'observed': [describe(row, kind) for row in matches],
                'accepted_variants': [describe(row, kind) for row in supported_matches
                    if describe(row, kind) != describe(expected_row, kind)],
                'observed_quotes': [row['evidence']['quote'] for row in matches if 'evidence' in row]})
        unexpected = [describe(items[0], kind) for key, items in actual.items() if key not in expected]
        duplicates = [describe(items[0], kind) for items in actual.values() if len(items) > 1] if kind == 'nodes' else []
        result['categories'][kind] = {'expected_count': len(expected),
            'matched_count': sum(row['status'] == 'matched' for row in rows),
            'rows': rows, 'unexpected': sorted(unexpected), 'duplicate_nodes': duplicates}
    return result


def score_experiment(directory, *, policy_path=None):
    """Score one isolated production experiment using its frozen source and answer key.

    Args:
        directory: Experiment folder containing manifest.json, source.txt, expected_graph.json
            and a fresh runs/ output root created by the graph evaluation runner.
        policy_path: Optional explicit policy for a new assessment of unchanged artifacts.
            None uses the experiment's frozen policy or its original legacy strict rules.

    Returns:
        A JSON-compatible PASS/FAIL/INCOMPLETE result. Missing execution, altered frozen
        inputs, truncated inventories or incomplete capture cannot pass. No model is called.
        Malformed artifact structures raise an input error to the CLI instead of passing.
    """
    manifest = json.loads((directory / 'manifest.json').read_text())
    source = (directory / 'source.txt').read_bytes()
    reference_bytes = (directory / 'expected_graph.json').read_bytes()
    if (hashlib.sha256(source).hexdigest() != manifest['source_sha256']
            or hashlib.sha256(reference_bytes).hexdigest() != manifest['reference_sha256']):
        raise ValueError('Frozen source or answer key changed after experiment creation')
    reference = json.loads(reference_bytes)
    policy, policy_sha256, policy_origin = None, None, 'legacy'
    frozen_policy = directory / 'comparison_policy.json'
    if 'comparison_policy_sha256' in manifest:
        frozen_bytes = frozen_policy.read_bytes()
        if hashlib.sha256(frozen_bytes).hexdigest() != manifest['comparison_policy_sha256']:
            raise ValueError('Frozen comparison policy changed after experiment creation')
        policy = ComparisonPolicy.model_validate_json(frozen_bytes)
        policy_sha256, policy_origin = manifest['comparison_policy_sha256'], 'frozen'
    if policy_path is not None:
        selected_bytes = policy_path.read_bytes()
        policy = ComparisonPolicy.model_validate_json(selected_bytes)
        policy_sha256, policy_origin = hashlib.sha256(selected_bytes).hexdigest(), 'explicit_rescore'
    reasons = []
    scope = manifest.get('execution_scope', 'research_workflow')
    unit_test = scope == 'knowledge_capture_unit'
    if manifest.get('capture_exit_code' if unit_test else 'research_exit_code') != 0:
        reasons.append('Capture component did not finish' if unit_test else 'Production command did not report successful completion')
    sessions = list((directory / 'runs').glob('*/session.json'))
    graph = {'nodes': [], 'sources': [], 'mentions': [], 'assertions': []}
    text, versions, run_status, model = '', set(), 'not_started', None
    if len(sessions) != 1:
        reasons.append('No unique saved research session')
    else:
        session = ResearchSession.model_validate_json(sessions[0].read_text())
        model = session.request.model
        run_status = session.status
        if unit_test:
            run_status = 'capture_incomplete' if session.knowledge.get('incomplete', True) else 'capture_complete'
            if session.plan is not None or session.visited_nodes != ['accumulate_knowledge']:
                reasons.append('Capture unit test unexpectedly executed other workflow stages')
        elif session.status != 'candidates_saved':
            reasons.append('Research did not finish: ' + session.status)
        windows = session.knowledge.get('windows', [])
        if (not session.request.accumulate_knowledge or not windows
                or session.knowledge.get('incomplete', True) or any(w['status'] != 'captured' for w in windows)):
            reasons.append('Broad graph capture is missing or incomplete')
        if len(session.documents) != 1 or session.documents[0].status != 'retrieved':
            reasons.append('Expected one retrieved fixture document')
        else:
            document = session.documents[0]
            if document.raw_sha256 != manifest['source_sha256']:
                reasons.append('Retrieved document is not the frozen source')
            text = read_verified_text(sessions[0].parent, document)
            if normalized(text) != normalized(source.decode('utf-8')):
                reasons.append('Saved text differs from the frozen source')
            database = directory / 'runs' / 'knowledge.sqlite3'
            if database.exists():
                graph = read_knowledge(database, limit=10000)
                if any(graph['truncated'].values()):
                    reasons.append('Graph inventory truncated')
                versions = {s['id'] for s in graph['sources'] if s['text_sha256'] == document.text_sha256
                    and s['location'] == (document.final_url or document.original_location)}
                if len(versions) != 1 or len(graph['sources']) != 1:
                    reasons.append('Unexpected or missing source versions in the isolated graph')
            else:
                reasons.append('Knowledge database missing')
    result = compare_graph(reference, graph, text, versions, policy)
    failed = bool(result['evidence_errors']) or any(
        c['matched_count'] != c['expected_count'] or c['unexpected'] or c['duplicate_nodes']
        for c in result['categories'].values())
    result.update(case_id=reference['case_id'], status='INCOMPLETE' if reasons else 'FAIL' if failed else 'PASS',
        incomplete_reasons=reasons, run_status=run_status, execution_scope=scope, source_sha256=manifest['source_sha256'],
        reference_sha256=manifest['reference_sha256'],
        requested_model=model, comparison_policy_id=policy.policy_id if policy else 'legacy-strict-v1',
        comparison_policy_sha256=policy_sha256, comparison_policy_origin=policy_origin,
        interpretation='Fixed-fixture graph contract; not general extraction accuracy or semantic review of arbitrary sources.')
    return result


def render_report(result):
    """Build a readable expected-versus-observed report so evaluation does not require log review.

    Args:
        result: Score containing per-record outcomes, unexpected records and execution gaps.

    Returns:
        Markdown with the overall outcome, every expected item and any extra/error details.
        The report never describes a fixture match as general model correctness.
    """
    lines = [f"# Knowledge graph evaluation: {result['status']}", '',
        f"Case: {result['case_id']} · Status: {result['run_status']}",
        f"Scope: {result.get('execution_scope', 'research_workflow')}", '', result['interpretation'], '']
    lines.extend([f"Requested model: {result.get('requested_model')}",
        f"Comparison policy: {result.get('comparison_policy_id', 'legacy-strict-v1')} ({result.get('comparison_policy_origin', 'legacy')})", ''])
    for kind, category in result['categories'].items():
        lines.extend([f"## {kind.title()}: {category['matched_count']}/{category['expected_count']} matched", '',
            '| Expected | Result |', '| --- | --- |'])
        for row in category['rows']:
            lines.append(f"| {row['expected'].replace('|', '/')} | {row['status'].upper()} |")
        lines.append('')
        for row in category['rows']:
            for variant in row.get('accepted_variants', []):
                lines.append(f"- Accepted variant: {variant}")
        lines.append('')
        for label, key in [('Unexpected', 'unexpected'), ('Duplicate node', 'duplicate_nodes')]:
            lines.extend(f"- {label}: {item}" for item in category[key])
        lines.append('')
    lines.extend(['## Execution and evidence issues', ''])
    lines.extend('- ' + issue for issue in result['incomplete_reasons'] + result['evidence_errors'])
    if not result['incomplete_reasons'] and not result['evidence_errors']:
        lines.append('None recorded. Missing/unsupported graph items, if any, are shown above.')
    return '\n'.join(lines) + '\n'
