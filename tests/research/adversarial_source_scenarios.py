"""Growing-source fixtures at external boundaries, never alternate schedulers.

Reuse the production CLI/SDK/HTTP harness, but vary source arrival, concurrency,
content identity and row order. Synthetic judgments establish mechanics only.
The oracle is the source text and persisted call ledger, not expected task names.
"""
from collections import Counter
from unittest.mock import patch

from tests.research import test_answer_controller_cli as controller
from tests.research.test_contract_completion import explicit_specification


class SourceScenario:
    """Own one independent, network-forbidden public-CLI investigation."""

    def __init__(self, owner, *, concurrency=3, arrivals=4, count=320, seed=109):
        """Build a long source and a stream of multi-passage competing sources.

        Args:
            self: Scenario retaining inputs and execution receipts.
            owner: unittest case registering all temporary-resource cleanup.
            concurrency: Actual CLI model concurrency, not a simulated worker count.
            arrivals: Useful search results introduced per search query.
            count: Literal source-row population, not an invented extracted count.
            seed: Deterministic row permutation for repeatable held-out orders.
        Returns:
            None. Sets up transport fixtures; no production function is replaced.
        """
        self.case = controller.AnswerControllerCLITests()
        self.case.setUp()
        owner.addCleanup(self.case.doCleanups)
        self.case.count = count
        self.case.schema = explicit_specification(count, supporting=True)
        self.case.install_sources(seed)
        self.concurrency, self.arrivals = concurrency, arrivals
        self.snapshots = []
        self.case.flow.search = self.search

    def search(self, query, max_results, monitor=None, **kwargs):
        """Introduce new readable sources without injecting answer rows through search.

        Args:
            self: Scenario tracking actual search dispatches and saved snapshots.
            query: Real scheduler-selected query; it does not change fixture facts.
            max_results: Production result cap, respected by this transport.
            monitor: Optional production observer, unused here.
            **kwargs: Provider options supplied by production code.
        Returns:
            New scope leads with enough text for several paid passage reads. Only
            external search/HTTP data is synthetic; selection and preparation run.
        """
        self.snapshots.append(self.case.cli.saved_session()[1])
        turn = len(self.snapshots)
        results = []
        for number in range(min(self.arrivals, max_results)):
            path = f'/lead-{turn}-{number}'
            body = ('This collection covers the requested scope.\n\n' +
                    '\n\n'.join(f'Background section {i} for source {turn}-{number}. ' +
                               'Context without additional requested records. ' * 45
                               for i in range(12)))
            self.case.flow.routes[('example.org', path)] = (
                200, {'Content-Type': 'text/plain'}, body.encode())
            results.append(dict(url='https://example.org' + path,
                title='Collection scope evidence', snippet='A specific scope lead.'))
        return results

    def duplicate(self, *, changed=False):
        """Add a distinct case-sensitive URL serving identical or changed content.

        Args:
            self: Scenario with its original /research route.
            changed: Change source values without changing row identities, proving
                URL similarity alone must never suppress new evidence.
        Returns:
            None. Adds a seed URL and real retrievable bytes, not a deduplication hint.
        """
        status, headers, body = self.case.flow.routes[('example.org', '/research')]
        if changed:
            body = body.replace(b'<td>Value', b'<td>Revised')
        self.case.flow.routes[('example.org', '/Research')] = (status, headers, body)
        with self.case.cli.urls.open('a') as stream:
            stream.write('\nhttps://example.org/Research\n')

    def run(self, *, budget=120, search=True):
        """Run the public production entry point with normal resource controls.

        Args:
            self: Prepared source/provider scenario with temporary run storage.
            budget: Shared model-call allowance, never reset or enlarged.
            search: Enable continuing source discovery or use seeds only.
        Returns:
            Terminal code, run path, saved session and terminal diagnostics. Source
            pacing sleeps alone are disabled; controller/clock/budget logic is real.
        """
        with patch('projectclarity.retrieval.retrieval_limits.time.sleep'):
            return self.case.flow.run_workflow('--design-answer', '--assess-coverage',
                '--stop-when-satisfied', *(['--search'] if search else []),
                '--max-search-queries', '12', '--max-requests', str(budget),
                '--model-concurrency', str(self.concurrency))


def extraction_costs(session):
    """Independently reconcile explicit versus source-preparation extraction costs.

    Args:
        session: Saved production run dictionary with task and attempt ledgers.
    Returns:
        Counts of model requests made by preparation and explicit-passage tasks;
        interrupted tasks without final outcomes are not represented as zero-cost.
    """
    costs = Counter()
    for task in session['workflow']['tasks'].values():
        if task['action'] != 'extract_knowledge' or not task.get('attempts'):
            continue
        before = task.get('before_model_requests')
        outcomes = task.get('outcomes', [])
        if before is None or not outcomes:
            continue
        after = outcomes[-1].get('model_requests_after')
        if after is None:
            continue
        kind = 'preparation' if task.get('source_id') and not task.get('batch_id') else 'passages'
        costs[kind] += after - before
    return dict(costs)


def repeated_content_reads(session):
    """Find paid rereads of identical acquired bytes and passage offsets.

    Args:
        session: Saved run with completed extraction and source-version receipts.
    Returns:
        Repeated (text hash, start, end) keys across different source identifiers.
        Changed bytes and different passage ranges are not treated as duplicates.
    """
    groups = {}
    for attempt in session['extraction']['attempts']:
        if attempt['status'] != 'completed':
            continue
        key = (attempt['text_sha256'], attempt['start'], attempt['end'])
        groups.setdefault(key, set()).add(attempt['source_id'])
    return {str(key): sorted(ids) for key, ids in groups.items() if len(ids) > 1}
