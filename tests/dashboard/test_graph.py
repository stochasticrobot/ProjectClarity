"""Exercise graph projection and HTTP delivery using genuine saved extraction runs.

Only the provider transport is synthetic. These tests establish discovery coverage,
source integrity, run isolation and routing, not semantic model accuracy.
"""
import hashlib
import http.client
import json
import subprocess
import threading
import unittest

from projectclarity.dashboard.graph import run_graph
from projectclarity.dashboard.server import make_server
from projectclarity.knowledge.graph_store import commit_observations
from tests.extraction import test_knowledge_extraction_integration as fixtures
from tests.extraction.extraction_test_fixtures import proposed_batch


class GraphTests(unittest.TestCase):
    """Keep incidental discoveries and unsupported evidence visible but distinguishable."""

    def setUp(self):
        """Create a real saved run with relationships, an orphan and raw result fields.

        Args:
            self: Test owner retaining temporary storage and cleanup callbacks.
        Returns:
            None. Production CLI writes the session with synthetic provider replies.
        """
        self.case = fixtures.KnowledgeExtractionIntegrationTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        batch = proposed_batch()
        batch['entities'].append(dict(entity_id='E3', name='<incidental topic>', entity_type='Topic'))
        batch['claims'][0]['research_roles'] = ['context']
        batch['claims'][0]['result_records'] = [dict(goal_id='G1', fields=[
            dict(name='person', value='Alice'), dict(name='affiliation', value='Lab X')])]
        code, _, err, self.directory, self.saved = self.case.run_extraction(batch=batch)
        self.assertEqual(code, 0, err)
        self.root = self.directory.parent
        self.run_id = self.directory.name

    def test_all_discoveries_not_report_only_and_no_writes(self):
        """Expose orphan entities, context relationships and pre-report record associations.

        Args:
            self: Real extraction fixture with no accepted report requirement.
        Returns:
            None. Every candidate is represented; reads leave session/source intact.
        """
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.directory.rglob('*') if p.is_file()}
        graph = run_graph(self.root, self.run_id)
        self.assertIn('<incidental topic>', {n['label'] for n in graph['nodes']})
        self.assertEqual({e['category'] for e in graph['edges']}, {'relationship', 'record_association'})
        self.assertEqual(graph['counts']['extracted_claims'], len(self.saved['extraction']['candidates']))
        self.assertEqual(len(graph['observations']), graph['counts']['extracted_claims'])
        ids = {n['id'] for n in graph['nodes']}
        self.assertTrue(all(e['source'] in ids and e['target'] in ids for e in graph['edges']))
        self.assertFalse(graph['warnings'])
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})
        (self.directory/'documents/S1/text.txt').write_text('Changed evidence')
        changed = run_graph(self.root, self.run_id)
        self.assertTrue(all(o['status'] == 'invalid_evidence' for o in changed['observations'].values()))
        self.assertEqual(len(changed['nodes']), len(graph['nodes']))

    def test_production_projection_attaches_explicit_person_record(self):
        """Use the saved-run entry point to link a record to its claim's named person.

        Args:
            self: Completed synthetic-provider run with genuine extraction receipts.
        Returns:
            None. One person node retains the original record fields, observations
            and all edges; source evidence and raw-mode records are unchanged.
        """
        path=self.directory/'session.json'
        saved=json.loads(path.read_text())
        for candidate in saved['extraction']['candidates']:
            for record in candidate['proposal'].get('result_records',[]):
                for field in record['fields']:
                    if field['name']=='person': field['name']='person_name'
        path.write_text(json.dumps(saved))
        raw=run_graph(self.root,self.run_id,consolidate=False)
        grouped=run_graph(self.root,self.run_id)
        alice=[n for n in grouped['nodes'] if n['label']=='Alice']
        self.assertEqual(len(alice),1)
        self.assertEqual(alice[0]['origin'],'person group')
        self.assertTrue(any(m.get('fields') for m in alice[0]['members']))
        self.assertEqual(raw['observations'],grouped['observations'])
        self.assertEqual({e['id'] for e in raw['edges']},{e['id'] for e in grouped['edges']})

    def test_database_sightings_are_run_scoped_and_keep_unreviewed_status(self):
        """Join this run's broader knowledge without leaking another run's discoveries.

        Args:
            self: Fixture creating real SQLite observations and repeated sightings.
        Returns:
            None. Shared observations survive, foreign-only entities stay out and
            same-name database/extraction entities are not silently merged.
        """
        db = self.root/'knowledge.sqlite3'
        source = dict(location='https://example.org/shared', text_sha256='a'*64, retrieved_at='2026-10-31')
        entities = [dict(entity_id='A', name='Alice', kind='person', evidence=[dict(quote='Alice')]),
            dict(entity_id='B', name='Other Lab', kind='organization', evidence=[dict(quote='Other Lab')])]
        assertions = [dict(subject_id='A', predicate='affiliated with', object_id='B', value=None,
            role=None, evidence=[dict(quote='Alice works at Other Lab')])]
        commit_observations(db, 'foreign', source, entities, assertions)
        commit_observations(db, self.run_id, source, entities, assertions)
        commit_observations(db, 'foreign', source,
            [dict(entity_id='C', name='Foreign only', kind='person', evidence=[dict(quote='Foreign only')])], [])
        graph = run_graph(self.root, self.run_id)
        self.assertNotIn('Foreign only', {n['label'] for n in graph['nodes']})
        self.assertGreaterEqual(sum(n['label']=='Alice' for n in graph['nodes']), 2)
        self.assertEqual(graph['counts']['database']['nodes'], 2)
        edge = next(e for e in graph['edges'] if e['label']=='affiliated with')
        self.assertEqual(edge['status'], 'source_anchored_unreviewed')

    def test_http_graph_and_local_assets(self):
        """Use actual HTTP routing and host checks for graph data and bundled assets.

        Args:
            self: Fixture with a completed run; no worker is launched by viewing it.
        Returns:
            None. Graph is available through its explicit endpoint; arbitrary paths
            and hostile hosts are rejected. Main-page graph navigation is present.
        """
        server = make_server(self.root, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path in ('/', '/graph', '/graph.js', '/graph.css', '/vendor/cytoscape.min.js', '/api/runs/'+self.run_id+'/graph', '/api/runs/'+self.run_id+'/graph?group=none'):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
                connection.request('GET', path)
                response = connection.getresponse(); body = response.read()
                self.assertEqual(response.status, 200, path)
                if path == '/': self.assertIn(b'id="graph-link"', body)
                if path.endswith('/graph') and path.startswith('/api/'):
                    self.assertEqual(json.loads(body)['run_id'], self.run_id)
                connection.close()
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
            connection.request('GET', '/api/runs/'+self.run_id+'/graph', headers={'Host':'evil.example'})
            response = connection.getresponse(); response.read()
            self.assertEqual(response.status, 403)
            connection.close()
            with self.assertRaises(Exception): run_graph(self.root, '../escape')
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_production_graph_controls_with_real_layout_engine(self):
        """Exercise real graph selection, filtering and evidence rendering in Node.

        Args:
            self: Saved-run fixture with explicit and record-derived edges.
        Returns:
            None. Node assertions verify safe markup and reversible neighborhood
            filtering; this test does not claim a visual browser inspection.
        """
        graph = run_graph(self.root, self.run_id)
        result = subprocess.run(['node', 'tests/dashboard/graph_checks.js'],
            input=json.dumps(graph), text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
