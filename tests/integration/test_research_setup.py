"""Check that the application understands a question and asks for clarification when needed.

Controlled AI replies exercise requirements definition, the pause for user answers,
refinement and search preparation. The tests check that answers stay attached to
the right questions, saved work can continue and invalid or repeated actions do not
repeat planning. No real AI service is used, so these are checks of the setup
process rather than the quality of the questions a model might ask.
"""
import json
from pathlib import Path
import tempfile
import time
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from projectclarity.workflow.research_graph import build_setup_graph as build_graph
from projectclarity.ux.web_app import Sessions, create_app

PLAN = {'information_needed': [{'kind':'entity_type','name':'Company','description':'Identify companies that develop AI products.'}]}


class FakeCaller:
    """Request-capturing provider substitute; makes no network calls."""
    model = 'offline-test'

    def __init__(self, questions=None, fail=None):
        """Configure controlled questions and optional malformed output.

        Args:
            questions: Questions to return, defaulting to one genuine scope choice.
            fail: Stage to return malformed JSON for, or None.
        Returns:
            None; initializes isolated request history.
        """
        self.questions = ['Headquartered in Canada, or operating there?'] if questions is None else questions
        self.fail, self.calls = fail, []

    def __call__(self, stage, request, sid):
        """Provide deterministic data for actual workflow nodes.

        Args:
            stage: Requested stage name.
            request: Actual serialized model request.
            sid: Owning session ID.
        Returns:
            JSON fixture; malformed JSON when explicitly configured. Records all calls.
        """
        self.calls.append((stage, request, sid))
        if stage == self.fail:
            return '{}'
        if stage == 'search_queries':
            return json.dumps({'searches': [
                {'query': 'Canada AI companies', 'purpose': 'Direct company discovery.'},
                {'query': 'Canadian machine learning product developers', 'purpose': 'Alternative activity terminology.'}]})
        if stage == 'define':
            return json.dumps(PLAN)
        if stage == 'clarify':
            return json.dumps({'questions': self.questions})
        data = json.loads(request['messages'][0]['content'])
        return json.dumps({'research_question': 'AI companies headquartered in Canada. ' + data['answers'][0]['answer'], **PLAN})


class SetupTests(unittest.TestCase):
    """Exercise clarification branches, bounded calls and durable real entry points."""

    def settled(self, sessions, sid):
        """Wait briefly for an offline background graph to reach a stable state.

        Args:
            sessions: Test session manager.
            sid: Session UUID.
        Returns:
            Stable snapshot; fails the test after five seconds rather than hanging.
        """
        for _ in range(500):
            state = sessions.read(sid)
            if state['status'] != 'working':
                return state
            time.sleep(.01)
        self.fail('Offline workflow did not finish')

    def test_graph_interrupt_resumes_without_replanning(self):
        """Assert exact question-answer binding and only one refinement call on resume.

        Args: None.
        Returns: None; checks real LangGraph interrupt and model prompt file loading.
        """
        caller = FakeCaller()
        graph = build_graph(caller, InMemorySaver())
        config = {'configurable': {'thread_id': 'test'}}
        result = graph.invoke({'session_id': 'test', 'original_question': 'Canadian AI companies?'}, config)
        self.assertIn('__interrupt__', result)
        self.assertEqual([c[0] for c in caller.calls], ['define','clarify'])
        result = graph.invoke(Command(resume=['Headquartered only.']), config)
        self.assertEqual([c[0] for c in caller.calls], ['define','clarify','refine'])
        self.assertEqual(result['answers'], [{'question':caller.questions[0], 'answer':'Headquartered only.'}])
        data = json.loads(caller.calls[-1][1]['messages'][0]['content'])
        self.assertEqual(data['answers'], result['answers'])
        self.assertFalse(graph.get_state(config).next)

    def test_no_ambiguity_finishes_without_extra_call(self):
        """Ensure clear questions bypass human pause and definition revision.

        Args: None.
        Returns: None; asserts exactly two calls and unchanged question.
        """
        caller = FakeCaller(questions=[])
        graph = build_graph(caller, InMemorySaver())
        result = graph.invoke({'session_id':'x','original_question':'A precise question'}, {'configurable':{'thread_id':'x'}})
        self.assertNotIn('__interrupt__', result)
        self.assertEqual(result['research_question'], 'A precise question')
        self.assertEqual(len(caller.calls), 2)

    def test_excess_questions_and_bad_plan_fail(self):
        """Reject excessive ambiguity questions and invalid upstream requirements.

        Args: None.
        Returns: None; failed planning cannot trigger subsequent model calls.
        """
        for caller, count in [(FakeCaller(questions=['one','two','three']),2),(FakeCaller(fail='define'),1)]:
            graph = build_graph(caller, InMemorySaver())
            with self.assertRaises(ValueError):
                graph.invoke({'session_id':'x','original_question':'Question'}, {'configurable':{'thread_id':'x'}})
            self.assertEqual(len(caller.calls), count)

    def test_http_restart_resume_and_duplicate_protection(self):
        """Check actual HTTP routes survive restart, reject bad input and prevent replay.

        Args: None.
        Returns: None; persisted graph resumes with exactly one new model call.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = FakeCaller()
            sessions = Sessions(root, first)
            sid = str(uuid4())
            with TestClient(create_app(sessions)) as client:
                self.assertEqual(client.get('/').status_code, 200)
                self.assertEqual(client.post('/api/setup',json={'session_id':sid,'question':'AI companies?'}).status_code,202)
                self.assertEqual(self.settled(sessions,sid)['status'],'clarification')
                self.assertEqual(client.post('/api/setup',json={'session_id':sid,'question':'AI companies?'}).status_code,202)
                self.assertEqual(len(first.calls),2)
                self.assertEqual(client.post(f'/api/setup/{sid}/answers',json={'answers':[' ']}).status_code,422)
                self.assertEqual(client.post('/api/setup',headers={'Origin':'https://other.example'},json={'session_id':str(uuid4()),'question':'X'}).status_code,403)
            sessions.close()
            resumed = FakeCaller()
            sessions = Sessions(root, resumed)
            with TestClient(create_app(sessions)) as client:
                self.assertEqual(client.get(f'/api/setup/{sid}').json()['status'],'clarification')
                self.assertEqual(client.post(f'/api/setup/{sid}/answers',json={'answers':['Headquartered only.']}).status_code,202)
                final = self.settled(sessions,sid)
                self.assertEqual(final['status'],'ready')
                self.assertIn('Headquartered only.',final['research_question'])
                self.assertEqual([c[0] for c in resumed.calls],['refine'])
                self.assertEqual(client.post(f'/api/setup/{sid}/answers',json={'answers':['Again']}).status_code,409)
                self.assertEqual(len(resumed.calls),1)
            sessions.close()

    def test_search_pair_uses_saved_definition_once_and_survives_restart(self):
        """Prove query generation uses finalized context and cannot replay setup.

        Args: None.
        Returns: None; tests HTTP idempotence, persisted pairs and exact prompt inputs.
        """
        with tempfile.TemporaryDirectory() as tmp:
            caller = FakeCaller()
            sessions = Sessions(Path(tmp), caller)
            sid = str(uuid4())
            with TestClient(create_app(sessions)) as client:
                client.post('/api/setup', json={'session_id':sid, 'question':'Canadian AI companies?'})
                self.settled(sessions, sid)
                self.assertEqual(client.post(f'/api/setup/{sid}/searches').status_code,409)
                client.post(f'/api/setup/{sid}/answers', json={'answers':['Headquarters only.']})
                ready = self.settled(sessions, sid)
                self.assertEqual(client.post(f'/api/setup/{sid}/searches').status_code,202)
                client.post(f'/api/setup/{sid}/searches')
                for _ in range(500):
                    final = sessions.read(sid)
                    if final['status'] != 'search_working':
                        break
                    time.sleep(.01)
                self.assertEqual(final['status'],'queries_ready')
                self.assertEqual(len(final['searches']),2)
                self.assertEqual([c[0] for c in caller.calls],['define','clarify','refine','search_queries'])
                payload = json.loads(caller.calls[-1][1]['messages'][0]['content'])
                self.assertEqual(payload['research_question'],ready['research_question'])
                self.assertEqual(payload['information_needed'],ready['information_needed'])
                self.assertEqual(payload['answers'],ready['answers'])
            sessions.close()
            replacement = FakeCaller()
            sessions = Sessions(Path(tmp), replacement)
            self.assertEqual(sessions.prepare_searches(sid)['searches'],final['searches'])
            self.assertEqual(replacement.calls,[])
            sessions.close()

    def test_failed_query_keeps_completed_definition(self):
        """Keep a valid finalized definition when the next model response fails.

        Args: None.
        Returns: None; verifies failure status, saved inputs and no duplicate retry.
        """
        with tempfile.TemporaryDirectory() as tmp:
            caller = FakeCaller(questions=[], fail='search_queries')
            sessions = Sessions(Path(tmp), caller)
            sid = str(uuid4())
            with TestClient(create_app(sessions)) as client:
                client.post('/api/setup', json={'session_id':sid,'question':'Precise question'})
                ready = self.settled(sessions,sid)
                client.post(f'/api/setup/{sid}/searches')
                for _ in range(500):
                    final = sessions.read(sid)
                    if final['status'] != 'search_working':
                        break
                    time.sleep(.01)
                self.assertEqual(final['status'],'search_failed')
                self.assertEqual(final['research_question'],ready['research_question'])
                self.assertEqual(final['information_needed'],ready['information_needed'])
                self.assertNotIn('searches',final)
                self.assertEqual(client.post(f'/api/setup/{sid}/searches').status_code,409)
                self.assertEqual(len(caller.calls),3)
            sessions.close()

    def test_duplicate_queries_fail_without_rewriting_definition(self):
        """Reject trivial duplicate pairs and count errors rather than claiming breadth.

        Args: None.
        Returns: None; verifies case/punctuation/word-order checks and one valid pair.
        """
        from projectclarity.workflow.research_graph import validate_searches
        for queries in [['Canada AI companies'], ['Canada AI companies','companies, CANADA AI'], ['x','y','z']]:
            with self.assertRaises(ValueError):
                validate_searches(json.dumps({'searches':[{'query':q,'purpose':'Test'} for q in queries]}))
        result = validate_searches(json.dumps({'searches':[
            {'query':'Canada AI companies','purpose':'Direct'},
            {'query':'Canadian machine learning developers','purpose':'Alternative'}]}))
        self.assertEqual(len(result.searches),2)

    def test_failed_revision_never_claims_ready(self):
        """Keep invalid revised definitions out of the ready state.

        Args: None.
        Returns: None; user answers remain checkpointed but no new valid definition is claimed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Sessions(Path(tmp), FakeCaller(fail='refine'))
            with TestClient(create_app(sessions)) as client:
                sid=str(uuid4())
                client.post('/api/setup',json={'session_id':sid,'question':'AI companies?'})
                self.settled(sessions,sid)
                client.post(f'/api/setup/{sid}/answers',json={'answers':['Canadian headquarters']})
                self.assertEqual(self.settled(sessions,sid)['status'],'failed')
            sessions.close()


if __name__ == '__main__':
    unittest.main()
