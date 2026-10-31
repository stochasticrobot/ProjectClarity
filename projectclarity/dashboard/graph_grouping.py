"""Build reversible display groups for dashboard.graph.run_graph, not DB identities.

Organizations use exact labels or unambiguous quoted abbreviations. A person can
own structured records when one source and claim explicitly link the person name;
complete same-goal record tuples can compose those groups across display formats.
For example, a named researcher and that claim's affiliation record share a node,
but each original field and evidence status stays in members. Shared names alone
never establish person identity. This module has no I/O or model dependencies.
"""
import re
from collections import defaultdict

from projectclarity.knowledge.graph_store import normalized_name, stable_id
from projectclarity.result_identity import result_key, PERSON_FIELDS, person_label

ORGANIZATION_KINDS = frozenset({'organization', 'organisation', 'institution',
    'university', 'college', 'school', 'company', 'political party', 'party',
    'lab', 'laboratory', 'research lab', 'research laboratory', 'research institute',
    'academic institution', 'academic department', 'academic faculty',
    'university research institute', 'organization name', 'organisation name',
    'institution name', 'affiliation', 'affiliation name', 'party affiliation', 'employer'})


def consolidate_graph(nodes, edges, observations, person_values=(), organization_values=()):
    """Group organizations, provenance-linked person records and matching complete tuples.

    Args:
        nodes: Raw source-scoped display nodes; people, statements and full records
            are not eligible for organization grouping. Complete same-goal record
            tuples use a separate all-fields-match rule. Explicitly typed people
            may attach records through a unique same-source/name/claim match.
            Overlapping groups compose by original membership, not by new inference.
            Unknown types remain separate unless the complete-record rule applies.
        edges: Original edges, each carrying its own observation/status.
        observations: Evidence map; invalid evidence cannot establish an alias.
        person_values: Explicitly typed person labels for existing formatting rules.
        organization_values: Explicit organization labels protecting names from person rules.
    Returns:
        New nodes, rewired edges and a grouping ledger. Every raw node survives
        either directly or in a group's members; every original edge ID and all
        evidence remain available. The legacy ledger key grouped_observations
        counts grouped original node representations, not unique evidence claims.
        Group statuses are unions, never an upgrade of individual review status.
        No input mutation, model call or DB write. Inputs must follow run_graph's
        node/edge shape; malformed required keys raise instead of being discarded.
    """
    eligible = [n for n in nodes if not n.get('fields') and
        normalized_name(n['kind'].replace('_', ' ')) in ORGANIZATION_KINDS]
    names = defaultdict(list)
    for item in eligible:
        names[normalized_name(item['label'])].append(item)
    expansions, alias_evidence = defaultdict(set), defaultdict(set)
    acronyms = {name for name, rows in names.items() if any(
        re.fullmatch(r'[A-Z][A-Z0-9]{1,11}', row['label'].strip()) for row in rows)}
    for oid, observation in observations.items():
        if observation.get('status') == 'invalid_evidence':
            continue
        evidence = observation.get('evidence') or []
        if isinstance(evidence, dict):
            evidence = [evidence]
        for entry in evidence:
            quote = entry.get('quote', '') if isinstance(entry, dict) else str(entry)
            text = normalized_name(quote)
            for full in names.keys() - acronyms:
                for short in acronyms:
                    # Exact paired forms only; shared domains or matching initials
                    # alone cannot establish an abbreviation or equivalence.
                    pattern = r'(?<!\w)(?:' + re.escape(full) + r'\s*\(\s*' + re.escape(short) + r'\s*\)|' + re.escape(short) + r'\s*\(\s*' + re.escape(full) + r'\s*\))(?!\w)'
                    if re.search(pattern, text):
                        expansions[short].add(full)
                        alias_evidence[(short, full)].add(oid)
    aliases = {short: next(iter(full)) for short, full in expansions.items() if len(full) == 1}
    buckets = defaultdict(list)
    for name, items in names.items():
        buckets[aliases.get(name, name)].extend(items)
    replacements, grouped = {}, []
    for canonical, members in sorted(buckets.items()):
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda item: item['id'])
        key = stable_id('organization_group', canonical)
        labels = sorted({n['label'] for n in members})
        label = sorted(n['label'] for n in members if normalized_name(n['label']) == canonical)[0]
        evidence_ids = sorted({oid for (short, full), ids in alias_evidence.items()
            if full == canonical and aliases.get(short) == canonical for oid in ids})
        grouped.append(dict(id=key, label=label, kind='organization', source='',
            sources=sorted({n['source'] for n in members if n.get('source')}),
            origin='organization group', aliases=labels,
            observation_ids=sorted({oid for n in members for oid in n['observation_ids']}),
            statuses=sorted({s for n in members for s in n.get('statuses', [])}),
            members=members, alias_evidence_ids=evidence_ids,
            identity='Reversible organization display group: exact normalized labels and explicitly quoted aliases; not an independently resolved identity.'))
        replacements.update({n['id']: key for n in members})
    organization_groups = len(grouped)
    # A saved claim explicitly referencing both representations provides a local
    # bridge, not a cross-source or fuzzy identity decision. Failed claims can
    # still identify their own subject; their evidence status is never promoted.
    people = [n for n in nodes if not n.get('fields') and n.get('source') and
        normalized_name(n['kind']) in {'person', 'human', 'individual', 'researcher'}]
    attached = defaultdict(list)
    for record in nodes:
        person_names = {person_label(f['value']) for f in record.get('fields', [])
            if f['name'].casefold() in PERSON_FIELDS}
        if len(person_names) != 1:
            continue
        matches = [p for p in people if p['source'] == record.get('source') and
            person_label(p['label']) in person_names and
            set(p['observation_ids']) & set(record['observation_ids'])]
        if len(matches) == 1:
            attached[matches[0]['id']].append(record)
    for person in people:
        records = attached.get(person['id'], [])
        if not records:
            continue
        members = [person] + sorted(records, key=lambda n: n['id'])
        grouped.append(dict(id=person['id'], label=person['label'], kind='person',
            source=person['source'], sources=[person['source']], origin='person group',
            aliases=sorted({n['label'] for n in members}), members=members,
            observation_ids=sorted({oid for n in members for oid in n['observation_ids']}),
            statuses=sorted({s for n in members for s in n.get('statuses', [])}),
            identity='Person and attached records share the same source, normalized explicit person name and a saved claim reference with exactly one matching person. Record fields and evidence statuses remain separate; Cross-source grouping, if present, requires the separate complete-record matching rule; names alone never bridge sources.'))
        replacements.update({n['id']: person['id'] for n in members})
    person_groups = len(grouped) - organization_groups
    record_buckets = defaultdict(list)
    for item in nodes:
        if not item.get('fields') or not item.get('goal_id'):
            continue
        values = {f['name']: f['value'] for f in item['fields']}
        # A person's name alone is not enough for cross-source grouping. Every
        # accompanying field must match; conflicts/missing fields remain separate.
        if any(k.casefold() in PERSON_FIELDS or person_label(v) in person_values
                for k, v in values.items()) and len(values) < 2:
            continue
        key = (item['goal_id'], result_key(values, person_values, organization_values))
        record_buckets[key].append(item)
    for identity, members in sorted(record_buckets.items()):
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda item: item['id'])
        representative = min(members, key=lambda n: (',' in n['label'], len(n['label']), n['label']))
        key = stable_id('record_group', identity)
        grouped.append(dict(id=key, label=representative['label'], kind=representative['kind'],
            source='', sources=sorted({n['source'] for n in members if n.get('source')}),
            origin='record group', aliases=sorted({n['label'] for n in members}),
            fields=representative['fields'], goal_id=identity[0], members=members,
            observation_ids=sorted({oid for n in members for oid in n['observation_ids']}),
            statuses=sorted({s for n in members for s in n.get('statuses', [])}),
            identity='Matching complete structured records within one goal; only existing name-format normalization is applied, not fuzzy person matching.'))
        replacements.update({n['id']: key for n in members})
    # Compose overlapping person/record groups instead of letting one pass
    # undo another. Membership union uses only the explicit rules above.
    merged = []
    for group in grouped:
        ids = {n['id'] for n in group['members']}
        overlaps = [g for g in merged if ids & {n['id'] for n in g['members']}]
        if not overlaps:
            merged.append(group)
            continue
        parts = overlaps + [group]
        members_by_id = {n['id']: n for g in parts for n in g['members']}
        persons = sorted((n for n in members_by_id.values() if n in people), key=lambda n: n['id'])
        representative = next((g for g in parts if g['origin']=='person group'), parts[0])
        combined = dict(representative,
            members=sorted(members_by_id.values(),key=lambda n:n['id']),
            sources=sorted({n['source'] for n in members_by_id.values() if n.get('source')}),
            aliases=sorted({n['label'] for n in members_by_id.values()}),
            observation_ids=sorted({oid for n in members_by_id.values() for oid in n['observation_ids']}),
            statuses=sorted({status for n in members_by_id.values() for status in n.get('statuses',[])}),
            identity=' '.join(sorted({g['identity'] for g in parts})))
        if persons:
            combined.update(id=persons[0]['id'], label=persons[0]['label'], kind='person', origin='person group')
        merged = [g for g in merged if g not in overlaps] + [combined]
    grouped = merged
    replacements = {n['id']:g['id'] for g in grouped for n in g['members']}
    new_nodes = [n for n in nodes if n['id'] not in replacements] + grouped
    new_edges = [dict(e, source=replacements.get(e['source'], e['source']),
        target=replacements.get(e['target'], e['target']),
        original_source=e['source'], original_target=e['target']) for e in edges]
    return new_nodes, new_edges, dict(groups=len(grouped),
        organization_groups=sum(g['origin']=='organization group' for g in grouped),
        person_groups=sum(g['origin']=='person group' for g in grouped),
        record_groups=sum(g['origin']=='record group' for g in grouped), original_nodes=len(nodes),
        displayed_nodes=len(new_nodes), grouped_observations=len(replacements),
        ambiguous_aliases=sorted(short for short, full in expansions.items() if len(full)>1))
