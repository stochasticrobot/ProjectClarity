"""Prepare saved extraction evidence for the graph without asking a model to rewrite it.

Only accepted chunks named by the document pipeline's knowledge index are read.
Entities remain distinct source mentions: matching names do not establish identity.
The graph keeps whole assertions and quotations, including uncertainty and negation,
so an arrow must never be mistaken for an independently verified relationship.
"""
import hashlib
import json

from projectclarity.claim_review.claim_evidence_collection import checked_path
from projectclarity.evidence_selection.relevance_selection import prepare_knowledge
from projectclarity.workflow.research_rounds import round_snapshots


def evidence_rows(root, state):
    """Read all retained rounds and produce source-linked graph records.

    Args:
        root: Directory of this one saved research session.
        state: Saved session snapshot with current and historical run references.
    Returns:
        Entity and assertion lists with session-local stable identifiers. Invalid
        hashes, escaped paths or malformed accepted records raise before database
        writes. Missing indexes contribute no evidence. No provider calls occur.
    """
    entities, assertions = {}, {}
    for snapshot in round_snapshots(state):
        artifact = snapshot.get('run', {}).get('artifact_directory', 'run')
        folder = checked_path(root, artifact)
        index_path = folder / 'knowledge.json'
        if not index_path.exists():
            continue
        for entry in json.loads(index_path.read_text())['chunks']:
            raw = checked_path(folder, entry['artifact']).read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != entry['sha256']:
                raise ValueError('Saved extraction evidence has changed.')
            receipt = json.loads(raw)
            if receipt.get('status') not in ('checks_pass', 'checks_partial'):
                raise ValueError('The graph accepts only indexed accepted chunks.')
            knowledge = prepare_knowledge(receipt, digest)
            prefix = f"{state['session_id']}:{artifact}:{entry['artifact']}:{digest}"
            for entity in knowledge['entities']:
                key = prefix + ':' + entity['id']
                entities[key] = {'id': key, 'name': entity['name'], 'type': entity['type'],
                                 'subtype': entity['subtype'], 'session_id': state['session_id']}
            for item in knowledge['facts']:
                fact = item['fact']
                key = prefix + ':' + item['fact_id']
                assertions[key] = {
                    'id': key, 'subject': prefix + ':' + fact['subject_id'],
                    'object': prefix + ':' + fact['object_id'] if fact['object_id'] else None,
                    'predicate': fact['predicate'], 'value': fact['value'],
                    'statement': fact['statement'], 'quote': fact['quote'],
                    'source_url': entry['source_url'], 'session_id': state['session_id'],
                    'round': snapshot['round'], 'document_id': entry['document_id'],
                    'chunk_index': entry['chunk_index'], 'artifact_sha256': digest,
                    'source_fact_id': item['fact_id'],
                    'quote_rechecked': knowledge['literal_quotes_rechecked'],
                    'review_status': 'extracted assertion; not independently verified',
                }
    return list(entities.values()), list(assertions.values())
