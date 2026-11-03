"""Check that one faulty extracted fact does not erase usable facts beside it.

These examples include valid quotes, spacing differences, missing references and
unusable repair replies. They exercise the application's checks and its handling
of a correction attempt, including later use of accepted facts. The AI replies
are supplied by the tests, so passing proves the recovery rules behave as expected,
not that a real model will always correct its mistakes.
"""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from uuid import uuid4
from projectclarity.knowledge_extraction.fact_validation_and_repair import assess, recover, match_quote
from tests.integration.test_document_pipeline import FixtureRun, ExtractCaller
from projectclarity.evidence_selection.evidence_selection_stage import EvidenceSelectionStage
from projectclarity.evidence_selection.relevance_selection import prepare_knowledge

ENTITY={'id':'e','name':'Subject','type':'concept','subtype':None}


def fact(quote, **changes):
    """Create a generic source assertion fixture.

    Args: quote: Evidence; changes: Explicit overridden fields.
    Returns: Fresh fact, never dependent on company-specific semantics.
    """
    return dict(subject_id='e',predicate='states',value=quote,object_id=None,statement=quote,quote=quote,**{})|changes


def payload(facts, entities=None):
    """Serialize test extraction proposals.

    Args: facts: Proposed records; entities: Optional catalogue override.
    Returns: Complete JSON string.
    """
    return json.dumps({'entities':entities if entities is not None else [ENTITY],'facts':facts})


class RepairCaller:
    """Bounded repair stub with recorded requests, optionally failing transport."""
    model='fixture'
    def __init__(self, response):
        """Configure repair output.

        Args: response: JSON-ready result or exception.
        Returns: None; initializes attempt list.
        """
        self.response=response;self.calls=[]
    def __call__(self,stage,request,sid):
        """Record one call and return or raise the configured result.

        Args: stage: Label; request: Provider payload; sid: Session ID.
        Returns: JSON, or raises the configured exception.
        """
        self.calls.append((stage,request,sid))
        if isinstance(self.response,Exception):raise self.response
        return json.dumps(self.response)


class RecoveryTests(unittest.TestCase):
    """Check source offsets, isolation, repair bounds and accepted projection reuse."""
    def test_whitespace_offsets_not_fuzzy(self):
        """Preserve source spans across whitespace without changing substantive text.

        Args: None.
        Returns: None; ambiguity, numbers and punctuation changes are rejected.
        """
        source='Earlier. Trial\n dose\twas 10 mg. Later.'
        m=match_quote('Trial dose was 10 mg.',source)
        self.assertEqual(source[m['start']:m['end']],m['quote']);self.assertEqual(m['method'],'whitespace')
        for quote in ['Trial dose was 100 mg.','Trial dose was 10 mg!']:
            with self.assertRaises(ValueError):match_quote(quote,source)
        with self.assertRaises(ValueError):match_quote('Same','Same and Same')
        with self.assertRaises(ValueError):match_quote('A B','A\nB A\tB')
    def test_isolation_schema_references_and_malformed_envelope(self):
        """Only records depending on invalid structure or references are rejected.

        Args: None.
        Returns: None; accepted neighbour remains usable downstream.
        """
        result=assess(payload([fact('Event happened.'),fact('invented'),fact('Event happened.',subject_id='bad'),{'quote':'bad'}]),'Event happened.')
        self.assertEqual(len(result['parsed']['facts']),1);self.assertEqual(len(result['rejected']),3)
        data={'status':'checks_partial','parsed':result['parsed']}
        self.assertEqual(len(prepare_knowledge(data,'a'*64)['facts']),1)
        duplicates=[ENTITY,ENTITY,dict(ENTITY,id='other')]
        result=assess(payload([fact('Event happened.'),fact('Event happened.',subject_id='other')],duplicates),'Event happened.')
        self.assertEqual(len(result['parsed']['facts']),1)
        for text in ['{',json.dumps({'entities':[],'facts':None})]:
            with self.assertRaises(ValueError):assess(text,'')
    def test_bounded_correction_withdrawal_failure_stop(self):
        """Keep accepted originals through correction, withdrawal, failure and stop.

        Args: None.
        Returns: None; no full-chunk retry, accepted edits, or extra attempts.
        """
        original=payload([fact('First.'),fact('Wrong.'),fact('Unsupported.')]);source='First. Second.'
        response={'entities':[],'decisions':[{'id':'P2','action':'correct','reason':'Exact passage','fact':fact('Second.')},{'id':'P3','action':'withdraw','reason':'No evidence','fact':None}]}
        caller=RepairCaller(response);result=recover(original,source,caller,'sid','repair',lambda:False,lambda a,b:None)
        self.assertEqual([f['quote'] for f in result['parsed']['facts']],['First.','Second.']);self.assertEqual(len(caller.calls),1);self.assertFalse(result['unresolved']);self.assertEqual(len(result['withdrawn']),1)
        packet=json.loads(caller.calls[0][1]['messages'][0]['content']);self.assertEqual([x['id'] for x in packet['rejected_facts']],['P2','P3'])
        for response in [RuntimeError('transport failed'),{'entities':[],'decisions':[]}, {'entities':[],'decisions':[{'id':'P2','action':'correct','reason':'try','fact':fact('Still invented.')},{'id':'P3','action':'withdraw','reason':'absent','fact':None}]}]:
            caller=RepairCaller(response);result=recover(original,source,caller,'sid','repair',lambda:False,lambda a,b:None)
            self.assertEqual(result['parsed']['facts'],[fact('First.')]);self.assertTrue(result['unresolved']);self.assertEqual(len(caller.calls),1)
        mixed={'entities':[],'decisions':[{'id':'P2','action':'correct','reason':'Grounded','fact':fact('Second.')},{'id':'P3','action':'correct','reason':'Malformed','fact':{'quote':'Only quote'}}]}
        result=recover(original,source,RepairCaller(mixed),'sid','repair',lambda:False,lambda a,b:None)
        self.assertEqual(len(result['parsed']['facts']),2);self.assertEqual(len(result['unresolved']),1)
        caller=RepairCaller({});result=recover(original,source,caller,'sid','repair',lambda:True,lambda a,b:None)
        self.assertEqual(len(caller.calls),0);self.assertEqual(len(result['unresolved']),2)
    def test_real_graph_keeps_partial_facts_for_selection(self):
        """Exercise production graph, receipts, knowledge index and evidence selection.

        Args: None.
        Returns: None; failed repair retains accepted fact and no repeated attempts.
        """
        class Caller(ExtractCaller):
            def __call__(self,stage,request,sid):
                """Inject one rejected neighbour and one repair transport failure.

                Args: stage, request, sid: Production provider-call arguments.
                Returns: Fixture JSON except the single intentional repair failure.
                """
                if stage.startswith('extract-repair-'):
                    self.calls.append((stage,request,sid));raise RuntimeError('unavailable')
                raw=super().__call__(stage,request,sid)
                if stage.startswith('extract-'):
                    data=json.loads(raw);data['facts'].append(dict(data['facts'][0],quote='UNSUPPORTED'));return json.dumps(data)
                return raw
        with TemporaryDirectory() as tmp:
            root=Path(tmp);sid=str(uuid4());state={'session_id':sid,'research_question':'Question','information_needed':[],'searches':[{'query':'one','purpose':'a'},{'query':'two','purpose':'b'}]}
            caller=Caller();run=FixtureRun(root,state,lambda s:None,caller,Event());run.execute()
            self.assertEqual(state['run']['status'],'partial');self.assertEqual(state['run']['facts'],1);self.assertEqual(state['run']['facts_unresolved'],1)
            receipts=list(run.root.glob('document-*/chunk-*.json'));self.assertEqual(len(receipts),1)
            receipt=json.loads(receipts[0].read_text());self.assertEqual(receipt['status'],'checks_partial');self.assertEqual(receipt['repair']['status'],'failed')
            selection=EvidenceSelectionStage(root,state,lambda s:None,caller,Event());selection.execute();self.assertEqual(state['selection']['facts_selected'],1)
            self.assertEqual(sum(stage.startswith('extract-repair-') for stage,_,_ in caller.calls),1)
