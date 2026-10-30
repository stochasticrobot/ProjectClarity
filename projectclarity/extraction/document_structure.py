"""Choose passage-reading order using table and card boundaries from the parser.

A long directory may put useful records far beyond its introduction. These helpers
find passages containing parser-marked table or repeated-card sections and sample
those sections before repeatedly reading only the document's beginning. All
passages remain available; choosing an earlier reading position does not declare
that text relevant or skip the rest permanently. No model or source retrieval
runs here, and the ordering does not depend on a particular site's names.
"""
from bisect import bisect_right
from collections import Counter, defaultdict

from projectclarity.extraction.saved_source_passages import RECORD_START
from projectclarity.nodes.schedule_work.source_priority import source_priority


def section_probes(text, attempts):
    """Find a passage to sample from each parser-marked table or card section.

    Sampling different sections helps reading reach structured records in long documents
    instead of spending every early request on the introduction. The function does
    not decide whether the sampled records answer the question.

    Args:
        text: Hash-verified normalized source text with optional record markers.
        attempts: Serialized base passage receipts for this exact source version.
    Returns:
        Ranked probe receipts containing only batch IDs and structural counts.
        Larger collections come first, with source order breaking ties. Offsets,
        passage bodies and evidence are unchanged; prose-only sources return [].
    """
    base = sorted((a for a in attempts if not a.get('refinement_parent')), key=lambda a: a['start'])
    starts = [a['start'] for a in base]
    groups = defaultdict(list)
    for marker in RECORD_START.finditer(text):
        label = marker.group().strip()[8:-1]
        group = label.split('R', 1)[0] if label.startswith('T') else 'C'
        groups[group].append(marker.start())
    probes = []
    for group, positions in groups.items():
        if len(positions) < 2 or not base:
            continue
        index = bisect_right(starts, positions[0]) - 1
        if index >= 0 and positions[0] < base[index]['end']:
            probes.append({'section': group, 'record_count': len(positions),
                'batch_id': base[index]['batch_id'], 'start': positions[0]})
    return sorted(probes, key=lambda p: (-p['record_count'], p['start']))


def passage_order(session, attempts):
    """Order pending source passages so structured sections can be sampled early.

    Retain every passage for later reading; only the order changes. Parser markers,
    not site-specific keywords or expected answer names, identify the sections.

    Args:
        session: Run with a plan and hash-bound document-structure receipts.
        attempts: Serialized extraction attempts, including completed and child work.
    Returns:
        New ordered list, never changed receipts. Official hosts get first inspection;
        within each tier, sources with fewer base attempts
        come first. Enumeration goals probe each structural collection, then read
        remaining base passages in order; refinements follow base work. Legacy or
        unshaped runs preserve sequential probing. No work is removed or reattempted.
    """
    served = Counter(a['source_id'] for a in attempts
        if not a.get('refinement_parent') and a['status'] != 'not_attempted')
    shaped = bool(session.plan and any(g.result_shape for g in session.plan.goals))
    structures = session.workflow.get('document_structure', {}) if shaped else {}
    ranks = {}
    for sid, data in structures.items():
        for index, probe in enumerate(data.get('probes', [])):
            ranks.setdefault((sid, data['text_sha256'], probe['batch_id']), index)
    return sorted(attempts, key=lambda a: (
        source_priority(session, a['source_id']),
        bool(a.get('refinement_parent')), served[a['source_id']],
        ranks.get((a['source_id'], a['text_sha256'], a['batch_id']), float('inf')),
        a.get('start', 0)))
