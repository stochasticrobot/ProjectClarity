"""Verify human CAPTCHA handoff through HTTP, real subprocess IPC and research routing.

Only external search responses, downloads and model answers are fixtures. The real
worker protocol, retained client algorithm, session registry, web endpoints and
workflow execute. These tests do not establish live DuckDuckGo acceptance rates.
"""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
from fastapi.testclient import TestClient
from projectclarity.search.web_search import search_web
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.ux.web_app import create_app
from tests.integration.test_document_pipeline import FixtureRun
from tests.integration.test_web_query_count import QueryCountCaller
from tests.integration.test_research_graph import wait_for_completion


class CaptchaRun(FixtureRun):
    """Keep production search/IPC and replace only document acquisition with fixtures."""
    def __init__(self, root, state, publish, caller, stop):
        """Restore the real search callable after setting up offline document transport.

        Args: root, state, publish, caller, stop: Standard source-collection contract.
        Returns: None; installs real search routing without live external acquisition.
        """
        super().__init__(root,state,publish,caller,stop)
        self.search.search=search_web


def wait_challenge(client, sid, previous=None):
    """Read public state until a new challenge is visible or the bounded wait fails.

    Args: client: Production TestClient; sid: Session UUID; previous: Old challenge ID.
    Returns: Public snapshot containing a new challenge; never submits a decision.
    """
    for _ in range(500):
        state=client.get(f'/api/setup/{sid}').json()
        if state.get('captcha') and state['captcha']['id']!=previous:return state
        time.sleep(.01)
    raise AssertionError('No pending challenge: '+str(state.get('status')))


class SearchCaptchaTests(unittest.TestCase):
    """Exercise submit, renewal, skip, cancellation, restart and session isolation."""
    def test_http_retained_worker_submit_skip_cancel(self):
        """Run real HTTP setup through each decision with a live fixture subprocess.

        Args: None.
        Returns: None; verifies paused work, one-use decisions, scope, no secret
            exposure, actual extraction after submit/skip and no fallback after cancel.
            A fake monotonic clock proves activity resets the idle deadline, reads
            do not reset it, and expiry works without any connected browser.
        """
        original_popen=subprocess.Popen
        fixture=Path(__file__).parents[1]/'fixtures/captcha_worker.py'
        for action in ('submit','skip','cancel','timeout'):
            with self.subTest(action=action),TemporaryDirectory() as tmp:
                children=[];fallbacks=[]
                def spawn(command, **kwargs):
                    """Launch fixture HTTP replies inside the actual search subprocess.

                    Args: command: Production child command; kwargs: Unchanged IPC/env flags.
                    Returns: Real Popen; records child identity for termination checks.
                    """
                    child=original_popen([sys.executable,str(fixture)],**kwargs)
                    children.append(child);return child
                def fallback(query,limit,cancelled,provider):
                    """Return one offline Mwmbl lead if and only if the user skips.

                    Args: query, limit, cancelled, provider: Public search adapter inputs.
                    Returns: Successful candidate receipt; records the provider call.
                    """
                    fallbacks.append(provider)
                    return {'provider':provider,'results':[{'url':'https://example.org/doc','title':'Fixture','snippet':'Lead'}]}
                root=Path(tmp).resolve()
                caller=QueryCountCaller(questions=[])
                sessions=Sessions(root,caller,env_file=root/'missing.env',runner_factory=CaptchaRun)
                sid=str(uuid4())
                try:
                    with patch('projectclarity.search.web_search.subprocess.Popen',side_effect=spawn), \
                         patch('projectclarity.search.web_search.search_public',side_effect=fallback), \
                         patch('projectclarity.search.web_search.SEARCH_INTERVAL_SECONDS',0), \
                         patch('projectclarity.search.captcha_handoff.monotonic',return_value=100) as clock, \
                         TestClient(create_app(sessions)) as client:
                        client.post('/api/setup',json={'session_id':sid,'question':'Which companies build AI?',
                                    'auto_advance':True,'web_query_count':1,'research_rounds':1})
                        state=wait_challenge(client,sid)
                        challenge=state['captcha'];old=challenge['id']
                        self.assertEqual(len(children),1)
                        self.assertIsNone(children[0].poll())
                        self.assertEqual(state['run']['model_calls'],0)
                        self.assertEqual(fallbacks,[])
                        self.assertNotIn('private-fixture-token',json.dumps(state))
                        self.assertNotIn('images',challenge)
                        tile=f'/api/setup/{sid}/captcha/{old}/tiles/0'
                        self.assertEqual(client.get(tile).status_code,200)
                        self.assertEqual(client.get(tile.replace(sid,str(uuid4()))).status_code,404)
                        base=f'/api/setup/{sid}/captcha'
                        body={'challenge_id':old,'action':'skip' if action=='timeout' else action,'tiles':[0]}
                        self.assertEqual(client.post(base,json=body,headers={'origin':'https://example.org'}).status_code,403)
                        self.assertEqual(client.post(base,json={**body,'tiles':[True]}).status_code,422)
                        self.assertEqual(client.post(base,json={**body,'tiles':[9]}).status_code,422)
                        if action=='submit':
                            # A deliberately incorrect synthetic answer causes a new puzzle.
                            self.assertEqual(client.post(base,json={**body,'tiles':[]}).status_code,200)
                            state=wait_challenge(client,sid,old)
                            self.assertEqual(client.post(base,json=body).status_code,409)
                            self.assertEqual(client.get(tile).status_code,404)
                            body['challenge_id']=state['captcha']['id']
                            self.assertEqual(len(children),1)
                        if action == 'timeout':
                            clock.return_value = 129
                            self.assertEqual(sessions.captchas.snapshot(sid)['idle_seconds_remaining'], 1)
                            self.assertEqual(client.post(base,json={**body,'action':'activity','tiles':[]}).status_code,200)
                            clock.return_value = 131
                            self.assertEqual(sessions.captchas.snapshot(sid)['idle_seconds_remaining'], 28)
                            # No UI polling or decision: the server worker must expire itself.
                            clock.return_value = 160
                            deadline = time.monotonic() + 5
                            while children[0].poll() is None and time.monotonic() < deadline:
                                time.sleep(.02)
                            self.assertIsNotNone(children[0].poll())
                            self.assertEqual(client.post(base,json={**body,'action':'activity'}).status_code,409)
                        else:
                            self.assertEqual(client.post(base,json=body).status_code,200)
                        self.assertEqual(client.post(base,json=body).status_code,409)
                        terminal=wait_for_completion(client,sid)
                        self.assertNotIn('captcha',terminal)
                        self.assertEqual(terminal['status'],'run_cancelled' if action=='cancel' else 'report_completed')
                        self.assertEqual(fallbacks,['mwmbl'] if action in ('skip','timeout') else [])
                        self.assertEqual(len(children),1)
                        self.assertIsNotNone(children[0].poll())
                        if action=='submit':
                            folder=root/sid/terminal['run']['artifact_directory']
                            receipt=json.loads((folder/'search-1.json').read_text())
                            self.assertEqual(receipt['human_submissions'],2)
                            self.assertEqual(receipt['results'][0]['url'],'https://example.org/doc')
                            self.assertGreater(terminal['run']['model_calls'],0)
                finally:sessions.close()

    def test_shutdown_cancels_pending_challenge_without_replay(self):
        """Closing Sessions must release a human wait and kill its isolated worker.

        Args: None.
        Returns: None; verifies shutdown, absent stale overlay after reopening and
            no resumed search or answer when a session is read after restart.
        """
        original_popen=subprocess.Popen
        fixture=Path(__file__).parents[1]/'fixtures/captcha_worker.py'
        children=[]
        def spawn(command, **kwargs):
            """Start a real fixture child while retaining the production IPC options.

            Args: command: Original worker command; kwargs: Private process options.
            Returns: Live child tracked for shutdown assertions.
            """
            child=original_popen([sys.executable,str(fixture)],**kwargs);children.append(child);return child
        with TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();sid=str(uuid4())
            sessions=Sessions(root,QueryCountCaller(questions=[]),env_file=root/'absent',runner_factory=CaptchaRun)
            try:
                with patch('projectclarity.search.web_search.subprocess.Popen',side_effect=spawn), \
                     patch('projectclarity.search.web_search.SEARCH_INTERVAL_SECONDS',0),TestClient(create_app(sessions)) as client:
                    client.post('/api/setup',json={'session_id':sid,'question':'Which companies build AI?',
                                'auto_advance':True,'web_query_count':1,'research_rounds':1})
                    wait_challenge(client,sid)
                    sessions.close()
                    self.assertIsNotNone(children[0].poll())
            except Exception:
                sessions.close();raise
            reopened=Sessions(root,QueryCountCaller(questions=[]),env_file=root/'absent',runner_factory=CaptchaRun)
            try:
                self.assertNotIn('captcha',reopened.read(sid))
                self.assertEqual(len(children),1)
            finally:reopened.close()
