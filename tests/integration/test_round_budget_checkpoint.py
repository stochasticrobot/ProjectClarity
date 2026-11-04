"""Prove pre-ranking choices through real HTTP/graph routing with offline I/O.

Budget counters and provider/search responses are simulated. Persistence, graph
boundaries, actual stage dispatch, decision admission and round transitions are real.
"""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4
from fastapi.testclient import TestClient
from projectclarity.ux.web_app import create_app
from projectclarity.workflow.research_sessions import Sessions
from tests.integration.test_research_rounds import RoundCaller, RoundRun
from tests.integration.test_research_graph import wait_for_completion


class BudgetView:
    """Expose controlled settled accounting; never simulate provider billing."""
    def __init__(self, calls=51, tokens=0, estimated=0):
        """Set counters; Args: calls, tokens, estimated: test usage; Returns: None."""
        self.value=dict(calls_used=calls,call_limit=100,known_tokens=tokens,
                       estimated_tokens=estimated,token_limit=1000,unknown_usage=0)
    def snapshot(self, sid):
        """Read counters; Args: sid: unused fixture session; Returns: copied usage."""
        return deepcopy(self.value)
    def begin(self, sid, stop):
        """Accept workflow binding; Args: sid, stop: production inputs; Returns: None."""
    def close(self):
        """Finish fixture; Args: None; Returns: None; no background workers exist."""


class RoundCheckpointTests(unittest.TestCase):
    """Check threshold edges, spending-free waiting and explicit restart-safe choices."""
    def exercise(self, action, calls=51, tokens=0, estimated=0, restart=False):
        """Run two real fixture rounds around a controlled budget boundary.

        Args: action: Continue/report, or None when no pause is expected; calls,
            tokens, estimated: Snapshot usage; restart: Reopen Sessions while paused.
        Returns: None; asserts no pre-approval ranking, no duplicate execution,
            correct final round count and unchanged budget allowances.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);caller=RoundCaller();caller.budgets=BudgetView(calls,tokens,estimated)
            original=caller.budgets.snapshot('fixture')
            RoundRun.fetched=[];RoundRun.searches_seen=[]
            sessions=Sessions(root,caller,runner_factory=RoundRun)
            sid=str(uuid4())
            try:
                with TestClient(create_app(sessions)) as client:
                    response=client.post('/api/setup',json={'session_id':sid,'question':'Fixture companies',
                        'auto_advance':True,'research_rounds':2})
                    self.assertEqual(response.status_code,202)
                    state=wait_for_completion(client,sid)
                    if action is None:
                        self.assertEqual(state['status'],'report_completed')
                        self.assertEqual(state['current_round'],2)
                        return
                    self.assertEqual(state['status'],'link_ranking_waiting')
                    self.assertFalse(any(stage.startswith('link-ranking-') for stage,_,_ in caller.calls))
                    self.assertEqual(RoundRun.fetched,['https://example.org/1'])
                    choice=state['round_budget_decision']['id']
                    # The ordinary report button must not bypass a pending decision.
                    self.assertEqual(client.post(f'/api/setup/{sid}/report').json()['status'],'link_ranking_waiting')
                if restart:
                    sessions.close();sessions=Sessions(root,caller,runner_factory=RoundRun)
                with TestClient(create_app(sessions)) as client:
                    self.assertEqual(client.get(f'/api/setup/{sid}').json()['round_budget_decision']['id'],choice)
                    data={'decision_id':choice,'action':action}
                    stale=client.post(f'/api/setup/{sid}/round-budget',json={**data,'decision_id':'stale'})
                    self.assertEqual(stale.json()['status'],'link_ranking_waiting')
                    self.assertEqual(client.post(f'/api/setup/{sid}/round-budget',json=data).status_code,202)
                    final=wait_for_completion(client,sid)
                    self.assertEqual(final['status'],'report_completed',final.get('report'))
                    self.assertEqual(final['current_round'],2 if action=='continue' else 1)
                    count=len(caller.calls)
                    client.post(f'/api/setup/{sid}/round-budget',json=data)
                    self.assertEqual(len(caller.calls),count)
                    rankings=[stage for stage,_,_ in caller.calls if stage.startswith('link-ranking-')]
                    self.assertEqual(len(rankings),1 if action=='continue' else 0)
                    self.assertEqual(caller.budgets.snapshot(sid),original)
            finally:
                sessions.close()

    def test_report_now_skips_ranking_and_survives_restart(self):
        """Args: None. Returns: None; persistent choice skips all ranking calls."""
        self.exercise('report',restart=True)

    def test_continue_allows_only_next_round_then_skips_final_ranking(self):
        """Args: None. Returns: None; explicit approval permits next-round I/O."""
        self.exercise('continue')

    def test_token_usage_and_estimates_trigger_but_exact_half_does_not(self):
        """Args: None. Returns: None; either budget binds, with a strict >50% edge."""
        self.exercise('report',calls=0,tokens=450,estimated=51)
        self.exercise(None,calls=50,tokens=500)
