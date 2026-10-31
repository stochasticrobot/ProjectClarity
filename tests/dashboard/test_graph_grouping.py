"""Exercise pure display grouping with synthetic records, not live-model claims.

Protect organization aliases, unique provenance-linked person records and complete
record composition against evidence loss or name-only conflation. The production
saved-run path is checked separately by test_graph.py; these fixtures prove grouping
rules and preservation, not whether an extracted real-world assertion is true.
"""
import copy
import unittest

from projectclarity.dashboard.graph_grouping import consolidate_graph


def entity(key, name, kind='organization', **extra):
    """Build an independent saved display record for identity-boundary tests.

    Args:
        key: Unique original record ID, also used as a distinct source label.
        name: Untrusted extracted label.
        kind: Saved type; people and unknown types must remain separate.
        **extra: Optional record fields or metadata exercising exclusion rules.
    Returns:
        Fresh fixture node with a source-specific observation link.
    """
    return dict(id=key, label=name, kind=kind, source='https://example.org/'+key,
        origin='extraction', observation_ids=[key], statuses=['needs_review'], **extra)


class OrganizationGroupingTests(unittest.TestCase):
    """Assert exact provenance accounting and conservative alias eligibility."""

    def test_exact_types_and_explicit_alias_preserve_all_observations_and_edges(self):
        """Unite organization spellings/type variants without mutating evidence.

        Args:
            self: Test owner comparing original and grouped record inventories.
        Returns:
            None. All member IDs, edge IDs and endpoint provenance survive;
            same-name people, suborganizations and full records remain separate.
        """
        nodes = [entity('a', 'Example University', 'university'),
            entity('b', ' EXAMPLE  UNIVERSITY ', 'organization_name'),
            entity('c', 'EU'), entity('p1', 'Alex Smith', 'person'),
            entity('p2', 'Alex Smith', 'person'), entity('lab', 'Lab at Example University'),
            entity('r', 'Example University', fields=[dict(name='city',value='York')])]
        observations = {'a':dict(status='needs_review', evidence=[dict(quote='Example University (EU) is a university.')])}
        edges = [dict(id='e1',source='p1',target='a',status='supported',observation_id='x'),
            dict(id='e2',source='p2',target='c',status='invalid_evidence',observation_id='y')]
        before = copy.deepcopy((nodes,edges,observations))
        result, rewired, ledger = consolidate_graph(nodes,edges,observations)
        group = next(n for n in result if n.get('members'))
        self.assertEqual({m['id'] for m in group['members']}, {'a','b','c'})
        self.assertEqual(set(group['observation_ids']), {'a','b','c'})
        self.assertEqual(group['alias_evidence_ids'], ['a'])
        self.assertEqual({n['id'] for n in result if not n.get('members')}, {'p1','p2','lab','r'})
        self.assertEqual([e['id'] for e in rewired], ['e1','e2'])
        self.assertEqual([e['original_target'] for e in rewired], ['a','c'])
        self.assertEqual([e['status'] for e in rewired], ['supported','invalid_evidence'])
        self.assertEqual(len({e['target'] for e in rewired}),1)
        self.assertEqual(ledger['original_nodes'], len(nodes))
        self.assertEqual(before, (nodes,edges,observations))

    def test_ambiguous_or_invalid_aliases_and_similar_names_do_not_merge(self):
        """Refuse acronym inference, conflicting expansions and substring matches.

        Args:
            self: Fixture with two unrelated organizations sharing an acronym.
        Returns:
            None. No ambiguous alias joins groups; invalid evidence cannot
            establish an alias, and punctuation/initial similarity is not enough.
        """
        nodes = [entity('a','Northern Institute'),entity('b','National Institute'),
            entity('c','NI'),entity('d','John K Smith','person'),entity('e','John Kenneth Smith','person')]
        for status, quote, ambiguous in [
            ('invalid_evidence','Northern Institute (NI)',[]),
            ('needs_review','Northern Institute and NI are mentioned.',[]),
            ('needs_review','Northern Institute (NI); National Institute (NI).',['ni'])]:
            with self.subTest(status=status,quote=quote):
                result, _, ledger=consolidate_graph(nodes,[],{'q':dict(status=status,evidence=[dict(quote=quote)])})
                self.assertEqual(len(result),len(nodes))
                self.assertEqual(ledger['ambiguous_aliases'],ambiguous)

    def test_complete_records_normalize_format_but_keep_different_context(self):
        """Reuse established name formatting only when every record field agrees.

        Args:
            self: Generic researcher records, not a Parliament-specific heuristic.
        Returns:
            None. Same full tuple groups; different employer, partial names,
            other goals and name-only person records stay separate.
        """
        nodes = []
        for key, name, employer, goal in [('a','Smith, Dr. Alex','North Lab','G1'),
                ('b','Alex Smith','North Lab','G1'), ('c','Alex Smith','South Lab','G1'),
                ('d','A. Smith','North Lab','G1'), ('e','Alex Smith','North Lab','G2')]:
            nodes.append(entity(key,name,'researcher',goal_id=goal,fields=[
                dict(name='full_name',value=name),dict(name='employer',value=employer)]))
        nodes += [entity(k,'Alex Smith','person',goal_id='G1',fields=[
            dict(name='name',value='Alex Smith')]) for k in ('f','g')]
        result, _, ledger = consolidate_graph(nodes,[],{},person_values={'alex smith'})
        groups=[n for n in result if n.get('members')]
        self.assertEqual(len(groups),1)
        self.assertEqual({n['id'] for n in groups[0]['members']},{'a','b'})
        self.assertEqual(ledger['record_groups'],1)
        self.assertEqual({m['id'] for n in result for m in n.get('members',[n])},
            {n['id'] for n in nodes})

    def test_small_source_fragment_joins_the_full_party_neighborhood(self):
        """Keep a 14-record secondary page from becoming a separate party universe.

        Args:
            self: Synthetic multi-source roster with 137 distinct complete records.
        Returns:
            None. One party has all 137 record neighbors, repeated observations
            add evidence rather than people, and every original edge is retained.
        """
        nodes=[entity('party-main','Sample Party','political_party'),
            entity('party-small','Sample Party','party affiliation')]
        edges=[]
        for source, count in [('main',137),('small',14)]:
            for i in range(count):
                name=f'Person{i} Example' if source=='main' else f'Example, Person{i}'
                key=f'{source}-{i}'
                nodes.append(entity(key,name,'member',goal_id='G1',fields=[
                    dict(name='full_name',value=name),dict(name='district',value=f'Ward{i}'),
                    dict(name='party',value='Sample Party')]))
                edges.append(dict(id='edge-'+key,source=key,target='party-'+source,
                    status='supported',observation_id=key))
        result, rewired, _=consolidate_graph(nodes,edges,{})
        party=next(n for n in result if n['label']=='Sample Party')
        neighbors={e['source'] for e in rewired if e['target']==party['id']}
        self.assertEqual(len(neighbors),137)
        self.assertEqual(len(rewired),151)
        self.assertEqual({e['id'] for e in rewired},{e['id'] for e in edges})

    def test_person_records_need_unique_shared_claim_source_and_explicit_name(self):
        """Attach record representations without fuzzy identity or status promotion.

        Args:
            self: Test owner exercising same-name ambiguity and source boundaries.
        Returns:
            None. Only explicitly linked records attach; invalid status and every
            original member, field and edge remain available in the projection.
        """
        person=entity('p','Alex Smith','person')
        record=entity('r','Alex Smith','researcher',goal_id='G1',fields=[
            dict(name='person_name',value='Alex Smith'),dict(name='affiliation',value='Lab')])
        record.update(source=person['source'],observation_ids=['p'],statuses=['invalid_evidence'])
        edges=[dict(id='e',source='r',target='lab',status='invalid_evidence')]
        for variant, grouped_expected in [('linked',True),('different_source',False),
                ('unlinked_claim',False),('ambiguous_person',False)]:
            with self.subTest(variant=variant):
                r=copy.deepcopy(record); nodes=[person,r,entity('lab','Lab')]
                if variant=='different_source': r['source']='https://elsewhere.test'
                if variant=='unlinked_claim': r['observation_ids']=['different']
                if variant=='ambiguous_person': nodes.append(dict(person,id='p2'))
                result,rewired,ledger=consolidate_graph(nodes,edges,{})
                self.assertEqual(ledger['person_groups'],int(grouped_expected))
                self.assertEqual({m['id'] for n in result for m in n.get('members',[n])},{n['id'] for n in nodes})
                self.assertEqual(rewired[0]['status'],'invalid_evidence')
                if grouped_expected:
                    group=next(n for n in result if n.get('members'))
                    self.assertEqual(group['members'][1]['fields'],record['fields'])
                    self.assertIn('invalid_evidence',group['statuses'])
                    self.assertEqual(rewired[0]['source'],'p')

    def test_person_attachment_composes_with_complete_record_format_groups(self):
        """Preserve full-tuple deduplication when records gain explicit person anchors.

        Args:
            self: Two name-format variants with distinct claim references.
        Returns:
            None. One reversible display group retains both people representations,
            both complete records and both original edges, without merging by name alone.
        """
        nodes=[];edges=[]
        for suffix,name in [('a','Alex Smith'),('b','Smith, Alex')]:
            p=entity('p'+suffix,name,'person')
            p['source']='https://example.org/roster'
            r=entity('r'+suffix,name,'member',goal_id='G1',fields=[
                dict(name='member_name',value=name),dict(name='district',value='Ward One')])
            r.update(source=p['source'],observation_ids=p['observation_ids'])
            nodes.extend([p,r]);edges.append(dict(id=suffix,source=r['id'],target='party'))
        nodes.append(entity('party','Example Party'))
        result,rewired,_=consolidate_graph(nodes,edges,{})
        groups=[n for n in result if n.get('members')]
        self.assertEqual(len(groups),1)
        self.assertEqual(len(groups[0]['members']),4)
        self.assertEqual(len({e['source'] for e in rewired}),1)
