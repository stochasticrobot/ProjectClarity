"""Prove source-bound omission repairs through the production extraction worker.

Provider replies are fixtures: these tests prove routing, retention and safeguards,
not a real model's ability to identify every entity relationship.
"""
from copy import deepcopy
import json
from queue import Queue
from threading import Event
import unittest

from projectclarity.knowledge_extraction.concurrent_extraction import extract_chunk
from projectclarity.knowledge_extraction.entity_omission_repair import repair_omissions
from projectclarity.knowledge_graph.evidence_projection import evidence_rows
from projectclarity.documents.document_pipeline import DocumentPipeline
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

SOURCE = 'Aster is headquartered in Paris. Mira belongs to Guild. The gathering is in Kyoto. Aster has 20 employees.'
ENTITIES = [{'id': 'aster', 'name': 'Aster', 'type': 'Organization', 'subtype': None},
            {'id': 'mira', 'name': 'Mira', 'type': 'Person', 'subtype': None},
            {'id': 'event', 'name': 'gathering', 'type': 'Event', 'subtype': None}]
FACTS = [dict(subject_id=s, predicate=p, value=v, object_id=None, statement=q, quote=q)
         for s, p, v, q in [
             ('aster', 'headquartered_in', 'Paris', 'Aster is headquartered in Paris.'),
             ('mira', 'member_of', 'Guild', 'Mira belongs to Guild.'),
             ('event', 'located_in', 'Kyoto', 'The gathering is in Kyoto.'),
             ('aster', 'employee_count', '20', 'Aster has 20 employees.')]]
PARSED = {'entities': ENTITIES, 'facts': FACTS}
REVIEW = {'entities': [dict(id=i, name=n, type=t, subtype=None) for i,n,t in
                      [('paris','Paris','Place'),('guild','Guild','Organization'),('kyoto','Kyoto','Place')]],
          'decisions': [dict(id=f'A{i+1}', action='link' if target else 'keep', object_id=target,
                             reason='Explicit named destination.' if target else 'Literal count.')
                        for i,target in enumerate(['paris','guild','kyoto',None])]}


class Caller:
    """Return controlled extraction/review replies and retain dispatched requests."""
    model = 'fixture'

    def __init__(self, review=None, stop=None):
        """Configure optional malformed reply, failure or stop after extraction.

        Args: review: Review dict/exception; stop: Optional cancellation event.
        Returns: None; creates isolated call history and fixture copies.
        """
        self.review = deepcopy(REVIEW if review is None else review)
        self.stop = stop
        self.calls = []

    def __call__(self, stage, request, sid):
        """Serve fixture data without external HTTP or model requests.

        Args: stage: Production label; request: Actual payload; sid: Session owner.
        Returns: JSON reply or configured review exception; may set stop on extraction.
        """
        self.calls.append((stage,request,sid))
        if stage.startswith('extract-omissions-'):
            if isinstance(self.review, Exception):
                raise self.review
            return json.dumps(self.review)
        if self.stop:
            self.stop.set()
        return json.dumps(PARSED)


class OmissionTests(unittest.TestCase):
    """Check production invocation, unchanged evidence and refusal of unsafe edits."""

    def worker(self, caller, stop=None):
        """Invoke the real bounded worker with one saved-source-shaped chunk.

        Args: caller: Fixture model transport; stop: Optional cancellation event.
        Returns: Worker receipt and queued operational events, with no file writes.
        """
        inbox = Queue()
        receipt = extract_chunk({'id':1,'final_url':'https://fixture.test/source','text_sha256':'a'*64},
                                {'index':0,'start':0,'end':len(SOURCE)}, SOURCE, [], caller,
                                'fixture', stop or Event(), inbox)
        return receipt, list(inbox.queue)

    def test_worker_links_places_membership_events_preserving_literals_and_originals(self):
        """Repair diverse entity destinations while preserving quote/statement bytes.

        Args: None.
        Returns: None; asserts two actual dispatches, three links and retained originals.
        """
        caller = Caller()
        receipt, events = self.worker(caller)
        self.assertEqual(receipt['status'],'checks_pass')
        self.assertEqual(len(caller.calls),2)
        self.assertEqual(sum(e[0]=='call' for e in events),2)
        self.assertEqual(json.loads(receipt['raw_text']),PARSED)
        self.assertEqual(receipt['entity_omissions']['original_parsed'],PARSED)
        self.assertEqual([f['object_id'] for f in receipt['parsed']['facts']],['paris','guild','kyoto',None])
        for old,new in zip(FACTS,receipt['parsed']['facts']):
            self.assertEqual({**new,'object_id':None},old)

    def test_malformed_failure_invention_redefinition_and_ambiguity_preserve_evidence(self):
        """Reject broken reviews and unsupported names without losing usable assertions.

        Args: None.
        Returns: None; failure and uncertain outcomes remain explicit, with one attempt.
        """
        bad_name=deepcopy(REVIEW);bad_name['entities'][0]['name']='Berlin'
        redefine=deepcopy(REVIEW);redefine['entities'].append(ENTITIES[0])
        missing=deepcopy(REVIEW);missing['decisions'].pop()
        ambiguous=deepcopy(REVIEW);ambiguous['decisions'][0].update(action='unresolved',object_id=None)
        for review in [RuntimeError('fixture failure'),missing,redefine,bad_name,ambiguous]:
            with self.subTest(review=str(review)[:50]):
                receipt,_=self.worker(Caller(review))
                self.assertEqual(receipt['status'],'checks_partial')
                self.assertEqual(receipt['parsed']['facts'][0],FACTS[0])
                self.assertTrue(receipt['entity_omissions']['unresolved'])
                self.assertEqual(json.loads(receipt['raw_text']),PARSED)

    def test_cancel_no_candidates_and_replay_are_bounded(self):
        """Do not dispatch after stop or review already-linked assertions again.

        Args: None.
        Returns: None; verifies source inputs untouched and existing entity reuse.
        """
        stop=Event();caller=Caller(stop=stop)
        receipt,_=self.worker(caller,stop)
        self.assertEqual(len(caller.calls),1)
        self.assertEqual(receipt['entity_omissions']['status'],'skipped_cancelled')
        linked={'entities':ENTITIES,'facts':[]}
        caller=Caller()
        output,audit=repair_omissions(linked,SOURCE,caller,'s','extract-omissions-test',lambda:False)
        self.assertEqual(audit['status'],'not_needed');self.assertFalse(caller.calls)
        parsed=deepcopy(PARSED);parsed['entities'].extend(REVIEW['entities'])
        review=deepcopy(REVIEW);review['entities']=[]
        output,audit=repair_omissions(parsed,SOURCE,Caller(review),'s','extract-omissions-test',lambda:False)
        self.assertEqual(len(output['entities']),len(parsed['entities']))
        self.assertEqual(parsed['facts'],FACTS)
        self.assertEqual(len(audit['patches']),3)

    def test_document_pipeline_persists_repairs_for_graph_projection(self):
        """Trace production coordinator receipts and graph rows from repaired evidence.

        Args: None.
        Returns: None; asserts indexed hashes, raw evidence retention and graph objects;
            only external acquisition/model replies are replaced with fixtures.
        """
        class Reader:
            def fetch(self, url):
                """Supply the synthetic source through the acquisition interface.

                Args: url: Fixture source URL.
                Returns: Text response and empty redirect history, without network I/O.
                """
                return url, {'content-type':'text/plain'}, SOURCE.encode(), []
        with TemporaryDirectory() as tmp:
            state={'session_id':str(uuid4()),'research_question':'Named relationships',
                   'information_needed':[], 'extraction_concurrency':1}
            runner=DocumentPipeline(Path(tmp),state,lambda snapshot:None,Caller(),Event(),
                                    paths=['https://fixture.test/source'],reader=Reader())
            runner.execute()
            index=json.loads((runner.root/'knowledge.json').read_text())
            self.assertEqual(len(index['chunks']),1)
            receipt=json.loads((runner.root/index['chunks'][0]['artifact']).read_text())
            self.assertEqual(receipt['entity_omissions']['original_parsed'],PARSED)
            entities,assertions=evidence_rows(Path(tmp),state)
            names={e['id']:e['name'] for e in entities}
            self.assertEqual([names.get(a['object']) for a in assertions],['Paris','Guild','Kyoto',None])
