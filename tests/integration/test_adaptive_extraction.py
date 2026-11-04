"""Prove adaptive sizing through the production document graph and model client.

Only provider HTTP and document acquisition are replaced. These fixtures exercise
real truncation typing, receipts, budgets, grounding, scheduling and knowledge
indexing. They do not establish live model recall or measured token savings.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock, Barrier
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from projectclarity.documents.document_pipeline import DocumentPipeline
from projectclarity.model_access.model_client import ModelCalls
from projectclarity.model_access.model_ledger import read_ledger
from projectclarity.claim_review.claim_evidence_collection import neighboring_passages, EvidenceCollector
from projectclarity.documents.web_acquisition import parse_document, read_document_text


class TextReader:
    """Serve exact fixture text while retaining real parsing and document storage."""

    def __init__(self, text):
        """Choose one complete source for every fixture URL.

        Args: text: Unicode source text without leading/trailing parser whitespace.
        Returns: None; performs no acquisition.
        """
        self.text = text

    def fetch(self, url):
        """Return a text response without contacting any external host.

        Args: url: Fixture source URL supplied by the real document pool.
        Returns: Final URL, headers, bytes and empty redirect history.
        """
        return url, {'content-type': 'text/plain'}, self.text.encode(), []


def provider_response(content, output=1000, stop='end_turn'):
    """Supply one grounded fact or a deliberately truncated provider-shaped reply.

    Args: content: Actual requested source span; output: Total output including
        simulated reasoning; stop: Provider stop reason, not a validator error.
    Returns: Response object consumed by the real ModelCalls transport.
    """
    quote = content  # Whole-span quotation is unambiguous even in repetitive fixtures.
    raw = json.dumps({'entities': [{'id': 'x', 'name': 'Fixture', 'type': 'organization', 'subtype': None}],
        'facts': [{'subject_id': 'x', 'predicate': 'states', 'value': quote, 'object_id': None,
                   'statement': quote, 'quote': quote}]}) if stop == 'end_turn' else '{"entities":['
    data = {'id': 'fixture', 'model': 'offline', 'stop_reason': stop,
            'usage': {'input_tokens': 10, 'output_tokens': output,
                      'output_tokens_details': {'thinking_tokens': output//2}}}
    return SimpleNamespace(id='fixture', model='offline', stop_reason=stop,
        content=[SimpleNamespace(type='text', text=raw)], model_dump=lambda **_: data)


class AdaptiveExtractionTests(unittest.TestCase):
    """Check bounded recovery, promotions, complete source coverage and stop/budget gates."""

    def run_case(self, strategy, text='A'*12000, concurrency=1, paths=1, cancel=False, budget=None):
        """Execute the production graph with programmable provider outcomes.

        Args: strategy: Function(number, content) returning (output_tokens, stop_reason)
            or raising a simulated provider error; text: Complete fixture document;
            concurrency: Saved worker cap; paths: Number of independent documents;
            cancel: Set Stop during the first provider response; budget: Optional
            request cap; when present, wait for a real budget pause and explicitly quit.
        Returns: Detached run, source text, receipts, knowledge, actual dispatch lengths
            and ledger/budget state. All temporary files are inspected before deletion.
        """
        with TemporaryDirectory() as tmp, patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'fixture'}), patch('projectclarity.model_access.model_client.Anthropic') as provider:
            root = Path(tmp); sid = str(uuid4()); folder = root/sid; folder.mkdir()
            state = {'session_id': sid, 'status': 'run_running', 'research_question': 'Fixture facts',
                     'information_needed': [], 'extraction_concurrency': concurrency,
                     'model_request_budget': budget or 1000, 'model_token_budget': 8_000_000}
            (folder/'session.json').write_text(json.dumps(state))
            stop = Event(); caller = ModelCalls(root, root/'unused'); caller.budgets.begin(sid, stop)
            messages = provider.return_value.__enter__.return_value.messages
            messages.count_tokens.return_value = SimpleNamespace(input_tokens=10)
            calls = []; lock = Lock()

            def send(**request):
                """Choose an outcome at the provider boundary, recording exact inputs.

                Args: request: Actual unchanged extraction request.
                Returns: Complete or truncated fixture reply; may request Stop or
                    propagate a strategy exception without modifying stage logic.
                """
                content = json.loads(request['messages'][0]['content'])['document_chunk']
                with lock:
                    calls.append(len(content)); number = len(calls)
                output, reason = strategy(number, content)
                if cancel and number == 1:
                    stop.set()
                return provider_response(content, output, reason)

            messages.create.side_effect = send
            snapshots = []

            def publish(snapshot):
                """Capture real coordinator snapshots without altering shared state.

                Args: snapshot: Mutable public stage state.
                Returns: None; saves detached metadata for progress assertions.
                """
                snapshots.append(json.loads(json.dumps(snapshot)))

            runner = DocumentPipeline(folder, state, publish, caller, stop,
                paths=[f'https://fixture.test/{i}' for i in range(paths)], reader=TextReader(text))
            if budget:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(runner.execute)
                    deadline = time.monotonic() + 5
                    try:
                        while time.monotonic() < deadline:
                            current = caller.budgets.snapshot(sid)
                            if current and current['paused']:
                                break
                            time.sleep(.01)
                        else:
                            self.fail('Recovery did not pause at the shared request budget')
                        self.assertEqual(len(calls), budget)
                        caller.budgets.decide(sid, current['revision'], 'quit')
                        future.result(timeout=5)
                    finally:
                        stop.set(); caller.budgets.close()
            else:
                runner.execute()
            receipts = {str(p.relative_to(runner.root)): json.loads(p.read_text())
                        for p in runner.root.glob('document-*/chunk-*.json')}
            knowledge = json.loads((runner.root/'knowledge.json').read_text())
            source_text = read_document_text(runner.root/'document-001/document.txt')
            self.assertEqual(source_text, text)
            for source in state['run']['sources']:
                leaves = sorted((c for c in source['chunks'] if c['status'] != 'split'), key=lambda c: c['start'])
                self.assertEqual(leaves[0]['start'], 0)
                self.assertEqual(leaves[-1]['end'], len(source_text))
                self.assertTrue(all(a['end'] == b['start'] for a, b in zip(leaves, leaves[1:])))
                ids = [c['index'] for c in source['chunks']]
                self.assertEqual(len(ids), len(set(ids)))
                for parent in (c for c in source['chunks'] if c['status'] == 'split'):
                    children = [c for c in source['chunks'] if c.get('parent_chunk_index') == parent['index']]
                    self.assertEqual(sorted(parent['child_chunk_indices']), sorted(c['index'] for c in children))
                    self.assertEqual(sum(c['end']-c['start'] for c in children), parent['end']-parent['start'])
            for ref in knowledge['chunks']:
                receipt = receipts[ref['artifact']]
                self.assertEqual(receipt['status'], 'checks_pass')
                self.assertEqual(json.loads(receipt['request']['messages'][0]['content'])['document_chunk'],
                                 source_text[receipt['start_char']:receipt['end_char']])
                for grounding in receipt['grounding']:
                    self.assertGreaterEqual(grounding['document_start'], receipt['start_char'])
                    self.assertLessEqual(grounding['document_end'], receipt['end_char'])
            collector = EvidenceCollector(folder, state, lambda: None)
            self.assertEqual(collector.errors, [])
            self.assertEqual(len(collector.knowledge_by_key), len(knowledge['chunks']))
            self.assertEqual(len(collector.passages), sum(c['status'] != 'split' for s in state['run']['sources'] for c in s['chunks']))
            return dict(run=state['run'], receipts=receipts, knowledge=knowledge,
                        calls=calls, ledger=read_ledger(folder), snapshots=snapshots,
                        budget=caller.budgets.snapshot(sid))

    def test_failure_ladder_and_two_clean_successes_promote_one_step(self):
        """Force three reductions, then prove guarded promotions and exact leaf coverage.

        Args: None.
        Returns: None; actual dispatches show 3000,1500,750,325 sizes and later growth;
            failed receipts never enter the accepted knowledge index.
        """
        def strategy(number, content):
            """Truncate the first three calls, then return small successful replies.

            Args: number: Dispatch order; content: Source text (not otherwise used).
            Returns: Provider output count and stop reason.
            """
            return (8192, 'max_tokens') if number <= 3 else (1000, 'end_turn')
        result = self.run_case(strategy)
        self.assertEqual(result['run']['status'], 'completed')
        self.assertEqual(result['calls'][:5], [3000, 1500, 750, 325, 325])
        self.assertEqual(result['run']['chunks_split'], 3)
        self.assertIn(3000, result['calls'][5:])
        self.assertEqual(result['ledger']['totals']['attempted'], len(result['calls']))
        self.assertEqual(result['budget']['calls_used'], len(result['calls']))
        self.assertEqual(result['run']['chunks_finished'], result['run']['chunks_total'])
        self.assertEqual(len(result['knowledge']['chunks']), len(result['calls']) - 3)
        messages = [e['message'] for e in result['run']['events']]
        self.assertTrue(any('increased to 750' in m for m in messages))
        self.assertTrue(any('increased to 1500' in m for m in messages))
        self.assertTrue(any('increased to 3000' in m for m in messages))

    def test_minimum_failure_is_finite_and_never_retries_identical_text(self):
        """Truncate every reply and prove recovery stops at 325-character leaves.

        Args: None.
        Returns: None; terminal spans are at most 325 characters, with finite attempts
            and exact coverage; unchanged short tails are never retried.
        """
        result = self.run_case(lambda n, text: (8192, 'max_tokens'), text='A'*3000)
        self.assertEqual(result['calls'][:3], [3000, 1500, 750])
        self.assertTrue(all(size <= 325 for size in result['calls'][3:]))
        self.assertLessEqual(len(result['calls']), 16)
        self.assertEqual(result['run']['status'], 'failed')
        self.assertEqual(result['run']['chunks_split'], 3)
        self.assertEqual(result['knowledge']['facts'], 0)
        leaves = [c for c in result['run']['sources'][0]['chunks'] if c['status'] != 'split']
        self.assertTrue(all(c['status'] == 'failed' and 'minimum' in c['error'] for c in leaves))
        tiny = self.run_case(lambda n, text: (8192, 'max_tokens'), text='A'*325)
        self.assertEqual(tiny['calls'], [325])

    def test_high_output_and_other_failures_do_not_promote_or_split(self):
        """Distinguish output pressure from malformed replies and transport errors.

        Args: None.
        Returns: None; reasoning-inclusive high output blocks promotion. Only an
            explicit provider max_tokens result produces children, never error text.
        """
        result = self.run_case(lambda n, text: (8192, 'max_tokens') if n == 1 else (6000, 'end_turn'))
        self.assertEqual(result['run']['sources'][0]['extraction_sizing']['chunk_characters'], 1500)
        self.assertTrue(all(size <= 1500 for size in result['calls'][1:]))
        for reason in ('pause_turn', 'refusal'):
            failed = self.run_case(lambda n, text: (1000, reason), text='A'*3000)
            self.assertEqual(failed['run']['chunks_split'], 0)
            self.assertEqual(failed['calls'], [3000])
        def error(number, content):
            """Simulate a network failure with misleading token-limit words.

            Args: number: Dispatch order; content: Requested text, both unused.
            Returns: Never; raises a generic error that must not trigger recovery.
            """
            raise RuntimeError('max_tokens mentioned, but no provider response')
        failed = self.run_case(error, text='A'*3000)
        self.assertEqual(failed['calls'], [3000])
        self.assertEqual(failed['run']['chunks_split'], 0)

    def test_stop_and_budget_quit_prevent_recovery_calls(self):
        """Cancel after a failed response and exhaust allowance before its children.

        Args: None.
        Returns: None; both stop paths retain the original failure and send no
            recovery generation, while retaining explicit uncovered source spans.
        """
        strategy = lambda n, text: (8192, 'max_tokens')
        stopped = self.run_case(strategy, cancel=True, text='A'*3000)
        self.assertEqual(stopped['calls'], [3000])
        self.assertEqual(stopped['run']['status'], 'cancelled')
        capped = self.run_case(strategy, budget=1, text='A'*3000)
        self.assertEqual(capped['calls'], [3000])
        self.assertEqual(capped['run']['status'], 'cancelled')
        self.assertEqual(capped['ledger']['totals']['attempted'], 1)
        self.assertEqual(capped['run']['model_calls'], 1)
        self.assertEqual(capped['run']['recovery_calls'], 0)

    def test_six_inflight_calls_and_independent_document_policies(self):
        """Hold a real six-worker wave, then make only the first document truncate.

        Args: None.
        Returns: None; confirms existing parallelism survives and stale completions
            cannot promote a changed policy. Other documents still begin at 3000.
        """
        barrier = Barrier(6, timeout=5)
        def strategy(number, content):
            """Synchronize only the initial provider wave and truncate its first call.

            Args: number: Actual dispatch sequence; content: Complete source span.
            Returns: Provider-shaped stop/output decision after the barrier opens.
            """
            if number <= 6:
                barrier.wait()
            return (8192, 'max_tokens') if number == 1 else (6000, 'end_turn')
        result = self.run_case(strategy, text='A'*21000, concurrency=6, paths=2)
        self.assertEqual(result['calls'][:6], [3000]*6)
        first, second = result['run']['sources']
        self.assertEqual(first['extraction_sizing']['chunk_characters'], 1500)
        self.assertEqual(second['extraction_sizing']['chunk_characters'], 3000)
        self.assertEqual(result['run']['status'], 'completed')

    def test_exact_boundary_neighbors_survive_nonsequential_chunk_ids(self):
        """Protect claim context when recovery children have later numeric IDs.

        Args: None.
        Returns: None; touching offsets win over IDs, with legacy fallback retained.
        """
        original = {'chunk_index': 20, 'start_char': 750, 'end_char': 1500}
        self.assertTrue(neighboring_passages(original, {'chunk_index': 4, 'start_char': 0, 'end_char': 750}))
        self.assertFalse(neighboring_passages(original, {'chunk_index': 21, 'start_char': 3000, 'end_char': 4500}))
        self.assertTrue(neighboring_passages({'chunk_index': 3}, {'chunk_index': 4}))

    def test_frozen_commons_roster_preserves_every_character_through_recovery(self):
        """Exercise actual roster line boundaries and Unicode with simulated truncation.

        Args: None.
        Returns: None; proves the real parser/scheduler/receipts/claim corpus retain
            all source spans. Invented facts and usage do not establish live MP recall.
        """
        path = Path(__file__).resolve().parents[1]/'fixtures/canadian_parliament/house_of_commons.html'
        text = parse_document(path.read_bytes(), 'text/html')['text']
        result = self.run_case(lambda n, content: (8192, 'max_tokens') if len(content) > 750 else (6000, 'end_turn'),
                               text=text, concurrency=6)
        self.assertEqual(result['run']['status'], 'completed')
        self.assertGreater(result['run']['chunks_split'], 0)
        self.assertTrue(all(r['end_char']-r['start_char'] <= 750 for r in result['receipts'].values()
                            if r['status'] == 'checks_pass'))
        self.assertEqual(result['run']['chunks_finished'], result['run']['chunks_total'])
