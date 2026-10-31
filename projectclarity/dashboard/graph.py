"""Read-only run graph spanning all saved discoveries, never just final report rows.

Extraction receipts and the run-scoped SQLite observation store remain authoritative.
The projection is rebuilt on refresh: no backfill, model call, canonical identity resolution or
semantic relationship inference occurs. Record-field edges are labelled associations.
The dashboard HTTP graph route calls run_graph; graph_grouping owns reversible
consolidation and static/graph.js owns filtering/layout. For example, a researcher
record adds affiliation-field edges even when it is absent from the final report.
"""
import json
import sqlite3
from collections import Counter

from projectclarity.knowledge.graph_store import connect_store, stable_id, normalized_name
from projectclarity.research_schemas import ResearchSession
from projectclarity.run_status_commands import run_directory
from projectclarity.nodes.assess_research_gaps.node import checked_candidates


def run_graph(root, run_id, *, consolidate=True):
    """Project every saved extraction observation and this run's database sightings.

    Args:
        root: Path to the private run directory root, also owning knowledge.sqlite3.
        run_id: Validated run identifier, never an arbitrary filesystem path.
        consolidate: Group organizations, provenance-linked people/records and complete tuples; False
            returns the original source-specific nodes without identity inference.
    Returns:
        Nodes, edges, evidence observations and counts with explicit identity and
        association semantics. Counts include record values and statements, not
        just unique entities. Sources are rechecked for evidence validity on each
        projection. Missing/corrupt session data raises; unavailable DB
        data is reported in warnings. No records or request budgets are changed.
    """
    directory = run_directory(root, run_id)
    session = ResearchSession.model_validate_json((directory / 'session.json').read_text())
    if session.session_id != run_id:
        raise ValueError('Run identity mismatch')
    data = session.model_dump(mode='json')
    sources = {s['source_id']: s.get('url') or str(s.get('path', 'Local document')) for s in data['sources']}
    nodes, edges, observations, warnings = {}, {}, {}, []

    def node(key, label, kind, source, origin, **details):
        """Register a stable display group without merging cross-source identities.

        Args:
            key: Stable, namespaced node key.
            label: Saved display text, never interpreted as markup.
            kind: Display category retained from the saved record or projection.
            source: Source location, or empty when unavailable.
            origin: Extraction, record, claim or database provenance category.
            **details: Additional inspectable non-secret metadata.
        Returns:
            Node key; repeated keys reuse the group and retain observation links.
        """
        nodes.setdefault(key, dict(id=key, label=label, kind=kind, source=source,
            origin=origin, observation_ids=[], **details))
        return key

    def edge(key, start, end, label, category, observation):
        """Retain one directed relation or clearly marked record association.

        Args:
            key: Unique observation-edge key; parallel contradictory edges survive.
            start: Existing subject node key.
            end: Existing object node key.
            label: Saved predicate or field label, not inferred meaning.
            category: relationship or record_association.
            observation: Evidence observation key determining visible status.
        Returns:
            None. Adds the edge, or records a warning for missing endpoints.
        """
        if start not in nodes or end not in nodes:
            warnings.append('A saved relationship has unresolved endpoints: ' + key)
            return
        edges[key] = dict(id=key, source=start, target=end, label=label,
            category=category, observation_id=observation,
            status=observations[observation]['status'])

    def attach(key, oid):
        """Associate evidence with a node while avoiding duplicate inspector entries.

        Args:
            key: Registered node identifier.
            oid: Registered observation identifier.
        Returns:
            None. Updates only this in-memory projection.
        """
        if key in nodes and oid not in nodes[key]['observation_ids']:
            nodes[key]['observation_ids'].append(oid)

    extraction = data.get('extraction') or {}
    entity_keys = {}
    for attempt in extraction.get('attempts', []):
        location = sources.get(attempt['source_id'], attempt['source_id'])
        for entity in (attempt.get('result') or {}).get('entities', []):
            key = stable_id('extracted', location, normalized_name(entity['name']), normalized_name(entity['entity_type']))
            node(key, entity['name'], entity['entity_type'], location, 'extraction',
                identity='Exact name/type display group within one source; identity unresolved')
            entity_keys[(attempt['batch_id'], entity['entity_id'])] = key
    valid = {}
    if session.extraction and session.plan:
        _, valid = checked_candidates(session, root)
    sections = {s['goal_id']: s for s in (data.get('answer_schema') or {}).get('sections', [])}
    for candidate in extraction.get('candidates', []):
        proposal = candidate['proposal']
        oid = 'claim:' + candidate['candidate_id']
        location = sources.get(candidate['source_id'], candidate['source_id'])
        status = candidate['adjudication_status'] if candidate['candidate_id'] in valid else 'invalid_evidence'
        observations[oid] = dict(id=oid, statement=proposal['statement'], status=status,
            source=location, evidence=candidate.get('evidence') or proposal.get('evidence', []),
            qualifications=proposal.get('qualifications', []), roles=proposal.get('research_roles', []),
            review_reason=candidate.get('adjudication_reason'), origin='extraction')
        linked = []
        for eid in proposal.get('entity_ids', []):
            key = entity_keys.get((candidate['batch_id'], eid))
            if key:
                linked.append(key)
        relationship = proposal.get('relationship')
        if relationship:
            start = entity_keys.get((candidate['batch_id'], relationship['subject_id']))
            end = entity_keys.get((candidate['batch_id'], relationship['object_id']))
            edge('relation:' + oid, start, end, relationship['predicate'], 'relationship', oid)
            linked.extend(k for k in (start, end) if k)
        for record in proposal.get('result_records', []):
            fields = record['fields']
            if not fields:
                continue
            section = sections.get(record['goal_id'], {})
            first = next((f for f in fields if f['name'] in section.get('identity_fields', []) and f['value']), fields[0])
            key = stable_id('record', location, record['goal_id'], sorted((f['name'], f['value']) for f in fields))
            node(key, first['value'], section.get('unit', 'structured record'), location, 'record',
                fields=fields, goal_id=record['goal_id'], identity='Exact structured record within one source; not a resolved entity')
            linked.append(key)
            for field in fields:
                if field is first:
                    continue
                target = stable_id('field', location, field['name'], field['value'])
                node(target, field['value'], field['name'].replace('_', ' '), location, 'record',
                    identity='Shared field value within one source, not an independently resolved entity')
                linked.append(target)
                edge(stable_id('association', oid, key, target), key, target,
                    field['name'].replace('_', ' '), 'record_association', oid)
        if not linked:
            linked.append(node('statement:' + oid, proposal['statement'], 'statement', location, 'claim'))
        for key in linked:
            attach(key, oid)

    database = root / 'knowledge.sqlite3'
    db_counts = Counter()
    if database.exists():
        try:
            with connect_store(database) as connection:
                records = {}
                for table in ('nodes', 'sources', 'mentions', 'assertions'):
                    rows = connection.execute(f'SELECT t.payload FROM {table} t JOIN sightings s ON s.record_id=t.id WHERE s.run_id=?', (run_id,))
                    records[table] = [json.loads(row[0]) for row in rows]
                    db_counts[table] = len(records[table])
                versions = {r['id']: r for r in records['sources']}
                for item in records['nodes']:
                    node('db:' + item['id'], item['name'], item['kind'], item['identity_scope'], 'database', identity=item['identity_status'])
                for item in [*records['mentions'], *records['assertions']]:
                    oid = 'db:' + item['id']
                    source = versions.get(item['source_version'], {}).get('location', '')
                    observations[oid] = dict(id=oid, statement=item.get('predicate', 'Entity mention'),
                        status=item['status'], source=source, evidence=item.get('evidence', []),
                        value=item.get('value'), role=item.get('role'), origin='database')
                    if 'node_id' in item:
                        attach('db:' + item['node_id'], oid)
                    else:
                        start = 'db:' + item['subject']
                        attach(start, oid)
                        if item['object']:
                            end = 'db:' + item['object']
                            attach(end, oid)
                            edge('edge:' + oid, start, end, item['predicate'], 'relationship', oid)
        except (sqlite3.Error, ValueError, OSError, KeyError) as exc:
            warnings.append('Broader knowledge store unavailable or incomplete: ' + type(exc).__name__)
    for item in nodes.values():
        item['statuses'] = sorted({observations[o]['status'] for o in item['observation_ids']}) or ['unreviewed_entity']
    displayed, connections = list(nodes.values()), list(edges.values())
    grouping = dict(groups=0, original_nodes=len(nodes), displayed_nodes=len(nodes),
        grouped_observations=0, ambiguous_aliases=[])
    if consolidate:
        from projectclarity.dashboard.graph_grouping import consolidate_graph
        from projectclarity.result_identity import person_labels, organization_labels
        displayed, connections, grouping = consolidate_graph(displayed, connections, observations,
            person_labels(extraction), organization_labels(extraction))
    return dict(grouping=grouping, run_id=run_id, question=data['request']['question'], nodes=displayed,
        edges=connections, observations=observations, warnings=warnings,
        counts=dict(nodes=len(displayed), edges=len(connections), observations=len(observations),
            extracted_claims=len(extraction.get('candidates', [])), database=dict(db_counts)),
        identity_policy='Matching organization labels and explicitly quoted aliases can be grouped for display. All original source records remain inspectable; people attach to their records only through an unambiguous shared claim, source and explicit person name. Complete matching records can bridge these display groups; names alone never merge people. Original fields and evidence statuses remain separate.',
        capture_enabled=data['request']['accumulate_knowledge'],
        scope='All saved extraction discoveries and this run’s broader-knowledge sightings; not limited to report results. Unextracted page content is not represented.')
