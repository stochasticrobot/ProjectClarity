"""Run the complete offline unittest suite and save results suitable for a CI check.

Use python -m evaluation.regression_suite --jobs 4 from the repository root after
installing the locked Python dependencies and Node.js. The runner discovers test
IDs, assigns non-overlapping groups to worker processes and records the outcome
of each test. It removes common credential environment variables from workers
and blocks non-loopback socket connections while allowing local dashboard tests.

A zero exit status requires every discovered test to pass, no missing required
regressions and no source changes during the run. Skips, expected failures and
missing results also block the gate. The printed output directory contains a
readable report, JSON outcomes, a test manifest, source hashes and worker logs.
No live model evaluation or deployment is performed. Passing simulated service
responses does not prove live provider acceptance or research accuracy.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_MODULES = (
    'tests.research.test_report_mapping',
    'tests.research.test_record_aggregation',
    'tests.research.test_release_invariants',
    'tests.research.test_controller_repair_boundaries',
    'tests.research.test_answer_design_recovery',
    'tests.research.test_model_recovery',
    'tests.research.test_concurrent_extraction',
    'tests.research.test_contract_completion',
    'tests.research.test_answer_policy',
    'tests.dashboard.test_dashboard',
)
# Require these individual test IDs as well as their modules, so removing an
# important case cannot leave a passing gate merely because its file still exists.
REQUIRED_TESTS = (
    'tests.research.test_record_aggregation.RecordAggregationTests.test_public_cli_obeys_single_and_multiple_completion_semantics',
    'tests.research.test_record_aggregation.RecordAggregationTests.test_unassigned_observations_never_count_or_fill_known_records',
    'tests.research.test_record_aggregation.RecordAggregationTests.test_coverage_markdown_and_dashboard_share_the_full_inventory',
    'tests.research.test_controller_repair_boundaries.ControllerRepairBoundaryTests.test_finished_event_publishes_complete_stable_exports',
    'tests.research.test_controller_repair_boundaries.ControllerRepairBoundaryTests.test_content_alias_survives_resume_without_replaying_uncertain_work',
    'tests.research.test_controller_repair_boundaries.ControllerRepairBoundaryTests.test_alias_binding_rejects_changed_versions_and_chains',
    'tests.research.test_controller_repair_boundaries.ControllerRepairBoundaryTests.test_paid_passages_are_inside_reserved_controller_waves',
    'tests.research.test_release_invariants.ReleaseInvariantTests.test_metadata_preparation_never_spends_model_requests',
    'tests.research.test_release_invariants.ReleaseInvariantTests.test_identical_content_does_not_receive_duplicate_paid_reads',
    'tests.research.test_release_invariants.ReleaseInvariantTests.test_case_variant_with_changed_content_is_not_suppressed',
    'tests.research.test_release_invariants.ReleaseInvariantTests.test_continuation_with_serial_burst_admissions',
    'tests.research.test_release_invariants.ReleaseInvariantTests.test_continuation_with_three_workers_and_burst_admissions',
    'tests.research.test_release_invariants.ReleaseInvariantTests.test_continuation_with_three_workers_and_gradual_admissions',
    'tests.research.test_answer_design_recovery.AnswerDesignRecoveryTests.test_both_live_failures_reject_then_correct_through_production',
    'tests.research.test_model_recovery.ModelRecoveryTests.test_provider_rejection_preserves_complete_message_and_body',
    'tests.research.test_contract_completion.ContractCompletionTests.test_adversarial_global_satisfaction_cannot_bypass_contract_gates',
    'tests.research.test_contract_completion.ContractCompletionTests.test_large_production_population_finishes_via_complete_reusable_batches',
)
LIVE_GATES = {
    'provider_acceptance': 'Not run: real provider must accept the current transmitted schemas.',
    'research_quality': 'Not run: held-out live research must satisfy independently scored answer requirements.',
    'live_efficiency': 'Not run: useful answer yield must be measured against calls/time on real sources.',
}


class LedgerResult(unittest.TextTestResult):
    """Track actually started/finished cases instead of assuming discovered tests ran."""

    def __init__(self, stream, descriptions, verbosity):
        """Initialize a result ledger alongside unittest's failure classifications.

        Args:
            self: Worker-local result instance.
            stream: unittest output stream.
            descriptions: Whether test docstrings appear in logs.
            verbosity: Standard unittest reporting level.
        Returns:
            None. Creates empty executed-case sets, never presumed successes.
        """
        super().__init__(stream, descriptions, verbosity)
        self.started_ids, self.finished_ids = set(), set()

    def startTest(self, test):
        """Record the concrete test entered by unittest.

        Args:
            self: Result ledger receiving lifecycle callbacks.
            test: Actual test case being entered.
        Returns:
            None. Preserves unittest counters and records its ID.
        """
        self.started_ids.add(test.id())
        super().startTest(test)

    def stopTest(self, test):
        """Record a finished case independently of its success/failure classification.

        Args:
            self: Result ledger receiving lifecycle callbacks.
            test: Test whose teardown has completed.
        Returns:
            None. Records completion; failure callbacks determine its verdict later.
        """
        self.finished_ids.add(test.id())
        super().stopTest(test)


def discover():
    """Collect the complete current unittest population without curating failures away.

    Args:
        None. Discovery is rooted in this repository's tests directory.
    Returns:
        Sorted unique test IDs. Import failures remain visible as FailedTest IDs.
    """
    stack = [unittest.defaultTestLoader.discover(str(ROOT / 'tests'), top_level_dir=str(ROOT))]
    ids = []
    while stack:
        item = stack.pop()
        if isinstance(item, unittest.TestSuite):
            stack.extend(item)
        else:
            ids.append(item.id())
    return sorted(set(ids))


def fingerprint():
    """Identify exactly which application, prompts, tests and dependencies were checked.

    Args:
        None. Only declared source trees and lockfiles are read; no run data or secrets.
    Returns:
        Relative-file SHA256 mapping suitable for stale-evidence detection.
    """
    paths = set()
    for tree in ['projectclarity', 'tests', 'evaluation']:
        paths.update((ROOT / tree).rglob('*.py'))
    paths.update((ROOT / 'projectclarity/prompts').glob('*.txt'))
    for tree in ['projectclarity/dashboard', 'tests/dashboard', 'tests/fixtures']:
        paths.update(p for p in (ROOT / tree).rglob('*')
                     if p.is_file() and p.suffix in {'.json', '.js', '.css', '.html', '.txt'})
    paths.update([ROOT / 'requirements.in', ROOT / 'requirements.txt'])
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths) if p.is_file()}


def guard_network():
    """Reject accidental non-loopback networking while permitting dashboard tests.

    Args:
        None. Called only in an isolated worker, never the user's application.
    Returns:
        None. Installs a connection guard; transport fixtures may deliberately
        replace sockets with offline substitutes. This is not a security sandbox.
    """
    original = socket.socket.connect

    def connect(sock, address):
        """Permit Unix/local dashboard sockets, refusing external destinations.

        Args:
            sock: Socket being connected by production code or test infrastructure.
            address: Socket address tuple or local Unix path.
        Returns:
            Original local connection result, or AssertionError before external I/O.
        """
        if isinstance(address, tuple) and address[0] not in {'127.0.0.1', '::1', 'localhost'}:
            raise AssertionError('Offline regression attempted an external connection')
        return original(sock, address)

    socket.socket.connect = connect


def run_worker(manifest, shard, output):
    """Run one immutable test partition and save every outcome including subtests.

    Args:
        manifest: JSON manifest path created by the parent.
        shard: Zero-based disjoint partition index.
        output: Existing report directory owned by the parent invocation.
    Returns:
        Zero only when every selected test passes without skipped/expected failures.
        Detailed failure messages remain in the worker log and JSON result.
    """
    guard_network()
    ids = json.loads(manifest.read_text())['shards'][shard]
    result = unittest.TextTestRunner(verbosity=2, resultclass=LedgerResult).run(
        unittest.defaultTestLoader.loadTestsFromNames(ids))
    outcomes = {name: dict(status='passed' if name in result.finished_ids else 'not_run', details=[])
                for name in ids}
    for status, rows in [('failed', result.failures), ('error', result.errors),
                         ('skipped', result.skipped), ('expected_failure', result.expectedFailures)]:
        for test, details in rows:
            name = getattr(test, 'test_case', test).id()
            row = outcomes.setdefault(name, dict(status=status, details=[]))
            row['status'] = status
            row['details'].append(dict(case=test.id(), explanation=str(details)))
    for test in result.unexpectedSuccesses:
        outcomes[test.id()] = dict(status='unexpected_success', details=[])
    passed = result.testsRun == len(ids) and all(row['status'] == 'passed' for row in outcomes.values())
    (output / f'shard-{shard}.json').write_text(json.dumps(dict(
        tests_run=result.testsRun, outcomes=outcomes, passed=passed), indent=2) + '\n')
    return 0 if passed else 1


def summarize(ids, outcomes, *, missing_modules=(), changed_files=()):
    """Fail closed when evidence is incomplete, stale or contains hidden failures.

    Args:
        ids: Complete expected test IDs for this invocation.
        outcomes: Collected test-ID mapping from finished worker reports.
        missing_modules: Required critical modules absent from discovery.
        changed_files: Source files changed while tests were running.
    Returns:
        Machine-readable offline and live gate decisions. Live readiness is never
        inferred from synthetic replies or from aggregate passing-test counts.
    """
    missing = sorted(set(ids) - set(outcomes))
    unexpected = sorted(set(outcomes) - set(ids))
    counts = dict(Counter(outcomes[name]['status'] for name in ids if name in outcomes))
    offline = (bool(ids) and not missing and not unexpected and not missing_modules
               and not changed_files and counts == {'passed': len(ids)})
    return dict(expected=len(ids), counts=counts, missing=missing, unexpected=unexpected,
        missing_critical_modules=list(missing_modules), changed_files=list(changed_files),
        offline_passed=offline, release_ready=False,
        live_gates={name: dict(status='not_run', explanation=reason) for name, reason in LIVE_GATES.items()})


def main(argv=None):
    """Run the complete offline suite or an internal manifest-bound worker.

    Args:
        argv: Optional CLI arguments; None reads the current process arguments.
    Returns:
        Process exit code: zero for a complete offline pass, one for failed/missing
        evidence, two for invalid invocation. No mode makes paid provider requests.
        Writes unique manifest, logs, fingerprints, JSON results and a short report.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--jobs', type=int, choices=range(1, 9), default=4)
    parser.add_argument('--worker', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--manifest', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    if args.worker is not None:
        return run_worker(args.manifest, args.worker, args.output)
    output = args.output or ROOT / '.projectclarity/diagnostics/regression-suite' / datetime.now(
        timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    ids, before = discover(), fingerprint()
    missing = [m for m in REQUIRED_MODULES if not any(name.startswith(m + '.') for name in ids)]
    missing.extend(name for name in REQUIRED_TESTS if name not in ids)
    manifest = output / 'manifest.json'
    manifest.write_text(json.dumps(dict(test_ids=ids, shards=[ids[i::args.jobs]
        for i in range(args.jobs)], required_modules=REQUIRED_MODULES, required_tests=REQUIRED_TESTS), indent=2) + '\n')
    (output / 'source-fingerprints.json').write_text(json.dumps(before, indent=2) + '\n')
    env = {key: value for key, value in os.environ.items()
           if not any(marker in key.upper() for marker in ['API_KEY', 'AUTH_TOKEN', 'ACCESS_TOKEN'])}
    children, started = [], time.monotonic()
    print(f'Running {len(ids)} tests in {args.jobs} disjoint workers. Reports: {output}', flush=True)
    for shard in range(args.jobs):
        log = (output / f'shard-{shard}.log').open('w')
        process = subprocess.Popen([sys.executable, '-m', 'evaluation.regression_suite',
            '--worker', str(shard), '--manifest', str(manifest), '--output', str(output)],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        children.append((process, log))
    exits = []
    try:
        for shard, (process, log) in enumerate(children):
            exits.append(process.wait())
            log.close()
            print(f'Worker {shard + 1}/{args.jobs} finished; exit={exits[-1]}', flush=True)
    finally:
        for process, log in children:
            if process.poll() is None:
                process.terminate()
                process.wait()
            log.close()
    outcomes = {}
    for path in sorted(output.glob('shard-*.json')):
        outcomes.update(json.loads(path.read_text())['outcomes'])
    after = fingerprint()
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    report = summarize(ids, outcomes, missing_modules=missing, changed_files=changed)
    report.update(worker_exits=exits, elapsed_seconds=round(time.monotonic() - started, 2), outcomes=outcomes)
    report['offline_passed'] = report['offline_passed'] and all(code == 0 for code in exits)
    (output / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    lines = ['# Regression gate', '', f"Offline gate: **{'PASS' if report['offline_passed'] else 'BLOCKED'}**",
        f"Expected tests: {len(ids)}; outcomes: {report['counts']}", '',
        '**Live provider acceptance, research quality and efficiency: NOT RUN.**', '', '## Non-passing cases', '']
    lines.extend(f"- `{name}` — {row['status']}" for name, row in outcomes.items() if row['status'] != 'passed')
    lines.extend(f'- `{name}` — missing result' for name in report['missing'])
    lines.extend(['', f'Source changes during execution: {changed}',
                  f'Missing required modules: {missing}', '', 'See results.json and shard logs for full evidence.'])
    (output / 'report.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'outcomes'}, indent=2), flush=True)
    return 0 if report['offline_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
