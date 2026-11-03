"""Follow selected links for a bounded number of research rounds.

Each round uses the ordinary document and claim-review stages. Earlier rounds
remain immutable, and only their reviewed report facts enter the combined report
input. This file manages round boundaries and provenance, not model decisions.
"""
from copy import deepcopy
import hashlib
import json

from projectclarity.claim_review.claim_evidence_collection import checked_path
from projectclarity.documents.web_acquisition import canonical_url
from projectclarity.knowledge_extraction.knowledge_extraction import save

ROUND_STAGES = ('run', 'selection', 'triage', 'investigation', 'link_ranking')


def round_snapshots(state):
    """Return completed rounds followed by the current round for provenance reads.

    Args: state: Saved session with optional round history (legacy sessions omit it).
    Returns: Ordered dictionaries; callers must not mutate their stage records.
    """
    return [*state.get('round_history', []),
            {'round': state.get('current_round', 1),
             **{key: state[key] for key in ROUND_STAGES if key in state}}]


def visited_sources(state):
    """Collect acquisition records across rounds to exclude previously seen URLs.

    Args: state: Saved session, including completed round snapshots.
    Returns: Source records from all rounds; includes unsuccessful attempts so
        automatic exploration does not repeatedly request a known failing URL.
    """
    return [source for item in round_snapshots(state)
            for source in item.get('run', {}).get('sources', [])]


def next_round_paths(state):
    """Choose unvisited selected destinations without another search or rating call.

    Args: state: Current completed ranking, round limit and prior acquisitions.
    Returns: At most ten DocumentPool entries, or none at the limit/no new links.
        Canonical original/final URLs are excluded; unknown redirect aliases can
        only be recognized after acquisition. Invalid saved URLs are ignored.
    """
    if state.get('current_round', 1) >= state.get('research_rounds', 1):
        return []
    seen = set()
    for source in visited_sources(state):
        for value in (source.get('url'), source.get('final_url')):
            try:
                seen.add(canonical_url(value or ''))
            except ValueError:
                pass
    paths = []
    for row in state.get('link_ranking', {}).get('selected', [])[:10]:
        try:
            url = canonical_url(row['url'])
        except (KeyError, ValueError):
            continue
        if url in seen:
            continue
        seen.add(url)
        paths.append({'path': url, 'title': row.get('link_title') or url,
                      'provenance': {'round': state.get('current_round', 1),
                                     'ranking': state['link_ranking'].get('artifact_directory'),
                                     'source_url': row.get('source_url')}})
    return paths


def begin_round(state, paths):
    """Archive completed stage references and prepare a new linked-document round.

    Args: state: Worker-owned session; paths: Nonempty selected DocumentPool inputs.
    Returns: None; mutates state but not files. The caller must initialize the
        document worker and publish before any fetching. Retry retains these paths
        and cannot replay first-round search. Earlier round artifacts stay intact.
    """
    if not paths:
        raise ValueError('A new round requires selected links.')
    state.setdefault('round_history', []).append(deepcopy(round_snapshots(state)[-1]))
    for key in ROUND_STAGES:
        state.pop(key, None)
        state.pop('resume_' + key + '_from', None)
    state['current_round'] = state.get('current_round', 1) + 1
    state['round_paths'] = deepcopy(paths)
    state.update(status='run_running', auto_select=True, stop_requested=False)


def report_evidence(root, state):
    """Materialize cumulative reviewed facts without changing investigation stores.

    Args: root: Session directory; state: Current and archived round stage records.
    Returns: (input directory, fact count, per-round coverage). Single-round runs
        retain their original input. Multi-round stores are content-addressed and
        reusable on report retry. Invalid context/counts or duplicate IDs within a
        round fail explicitly. Facts get round-qualified IDs and original lineage;
        no raw extraction or rejected claims are promoted. No model calls occur.
    """
    rounds = round_snapshots(state)
    if len(rounds) == 1:
        return (checked_path(root, state['investigation']['artifact_directory']),
                state['investigation']['report_knowledge'], [])
    facts, inputs, coverage = [], [], []
    for item in rounds:
        investigation = item['investigation']
        folder = checked_path(root, investigation['artifact_directory'])
        manifest = json.loads((folder / 'manifest.json').read_text())
        raw = (folder / 'report_knowledge.json').read_bytes()
        store = json.loads(raw)
        if (manifest['research_question'] != state['research_question']
                or manifest['requested_information'] != state['information_needed']
                or store['research_question'] != state['research_question']):
            raise ValueError('Round evidence belongs to different research requirements.')
        records = store['facts']
        if (len(records) != investigation['report_knowledge']
                or len({row['fact_id'] for row in records}) != len(records)):
            raise ValueError('Round report evidence count or identity is invalid.')
        inputs.append({'round': item['round'], 'artifact_directory': investigation['artifact_directory'],
                       'sha256': hashlib.sha256(raw).hexdigest(), 'facts': len(records)})
        coverage.append({'round': item['round'],
                         **{key: item.get(key, {}).get('status', 'not_started') for key in ROUND_STAGES},
                         'unreviewed_investigation': investigation.get('claims_total', 0)
                             - investigation.get('accepted', 0) - investigation.get('rejected', 0),
                         'unreviewed_triage': investigation.get('triage_unreviewed', 0)})
        for record in records:
            row = deepcopy(record)
            row['original_fact_id'] = row['fact_id']
            row['fact_id'] = f"round-{item['round']}:{row['fact_id']}"
            row['research_round'] = item['round']
            row['source']['run_artifact_directory'] = item['run']['artifact_directory']
            facts.append(row)
    payload = {'research_question': state['research_question'], 'facts': facts}
    manifest = {'research_question': state['research_question'],
                'requested_information': state['information_needed'], 'round_inputs': inputs,
                'round_coverage': coverage}
    digest = hashlib.sha256(json.dumps([payload, manifest], sort_keys=True).encode()).hexdigest()
    folder = root / ('report-evidence-' + digest)
    folder.mkdir(exist_ok=True, mode=0o700)
    for name, content in (('report_knowledge.json', payload), ('manifest.json', manifest)):
        path = folder / name
        if path.exists():
            stored = json.loads(path.read_text())
            redacted = stored.pop('credential_redaction_applied', False)
            if redacted or stored != content:
                raise ValueError('Saved cumulative report input changed.')
        else:
            save(path, content, '')
    return folder, len(facts), coverage
