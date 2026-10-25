"""Test the knowledge-capture component directly with a fixed document and answer key.

There is no planner, search, LangGraph orchestration or answer-candidate extraction.
Fixture setup supplies the saved-text input that this component normally receives
after retrieval. `run` invokes the actual capture function/model and SQLite store;
offline unit tests replace provider HTTP. `show` and `score` never call a model.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from langsmith import tracing_context

from evaluation.knowledge_graph_scoring import render_report, score_experiment
from evaluation.graph_comparison_policy import read_policy
from projectclarity.nodes.accumulate_knowledge.node import accumulate_knowledge
from projectclarity.research_schemas import Budget, DEFAULT_MODEL, FileSource, ResearchRequest, ResearchSession, RetrievedDocument, Usage
from projectclarity.session_storage import save_session


CASE_DIRECTORY = Path(__file__).parent / 'cases' / 'research_network'


def save_report(directory, *, policy_path=None):
    """Score saved production artifacts and persist readable and machine-readable results.

    Args:
        directory: Isolated evaluation directory with frozen inputs and research outputs.
        policy_path: Optional new comparison policy. Freeze it and save a separate assessment
            under policy_rescores/ without changing original reports, inputs or model outputs.

    Returns:
        The score dictionary. Write report.md and score.json and print the report and its
        location; a failed comparison is retained for inspection without a model retry.
    """
    destination = directory
    if policy_path is not None:
        read_policy(policy_path)
        destination = directory / 'policy_rescores' / uuid.uuid4().hex
        destination.mkdir(parents=True, exist_ok=False)
        policy_bytes = policy_path.read_bytes()
        (destination / 'comparison_policy.json').write_bytes(policy_bytes)
        policy_path = destination / 'comparison_policy.json'
    result = score_experiment(directory, policy_path=policy_path)
    if destination != directory:
        result['original_experiment'] = str(directory.resolve())
    report = render_report(result)
    (destination / 'score.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    (destination / 'report.md').write_text(report, encoding='utf-8')
    print(report)
    print('Readable report:', destination / 'report.md')
    return result


def capture_fixture(directory, args):
    """Supply fixed source text directly to the capture unit so planning cannot block its test.

    Args:
        directory: Isolated experiment folder containing the frozen UTF-8 source.txt fixture.
        args: Explicit model, request and reply-token settings for this one component test.

    Returns:
        Zero when capture reports all windows completed, otherwise one. Write fixture input
        receipts, call only the production accumulate_knowledge function, and retain its
        real SQLite outputs. No plan, retrieval service or answer extraction is invoked.
        Provider/configuration failures remain visible in capture receipts. Remote tracing
        stays disabled, as it is in the full application runner.
    """
    source = (directory / 'source.txt').read_bytes()
    text = source.decode('utf-8')
    digest = hashlib.sha256(source).hexdigest()
    root = directory / 'runs'
    run_id = uuid.uuid4().hex
    saved = root / run_id / 'documents' / 'S1'
    saved.mkdir(parents=True, exist_ok=False)
    (saved / 'raw.bin').write_bytes(source)
    (saved / 'text.txt').write_text(text, encoding='utf-8')
    location = str((directory / 'source.txt').resolve())
    now = datetime.now(UTC).isoformat()
    request = ResearchRequest(question='Capture the named entities and explicit observations in the supplied text.',
        plan_only=False, extract_knowledge=True, accumulate_knowledge=True, model=args.model or DEFAULT_MODEL,
        budget=Budget(max_requests=args.max_requests, max_output_tokens=args.max_output_tokens))
    session = ResearchSession(session_id=run_id, created_at=now, request=request,
        mode='plan_retrieve_extract', status='sources_retrieved',
        usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0),
        sources=[FileSource(source_id='S1', path=location)],
        documents=[RetrievedDocument(source_id='S1', source_kind='file', original_location=location,
            status='retrieved', attempted_at=now, acquired_at=now, raw_sha256=digest, text_sha256=digest,
            byte_count=len(source), character_count=len(text), raw_path='documents/S1/raw.bin',
            text_path='documents/S1/text.txt', parser='fixed_text_unit_fixture')])
    save_session(session, root)
    with tracing_context(enabled=False):
        result = accumulate_knowledge({'session': session}, root)['session']
    return 1 if result.knowledge.get('incomplete', True) else 0


def run_evaluation(args):
    """Freeze the source and answer key, then invoke only the knowledge-capture unit.

    Args:
        args: Parsed run options specifying model, output root and explicit request/token caps.

    Returns:
        Zero only for completed capture matching the expected graph; nonzero for differences
        or incomplete work. Save exact inputs, hashes and results without retries. The
        capture model receives only source windows and its normal capture prompt, not the key.
    """
    directory = args.output_dir / 'research_network' / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    source = (CASE_DIRECTORY / 'source.txt').read_bytes()
    reference = (CASE_DIRECTORY / 'expected_graph.json').read_bytes()
    policy_path = CASE_DIRECTORY / 'comparison_policy.json'
    read_policy(policy_path)
    policy_bytes = policy_path.read_bytes()
    (directory / 'source.txt').write_bytes(source)
    (directory / 'expected_graph.json').write_bytes(reference)
    (directory / 'comparison_policy.json').write_bytes(policy_bytes)
    manifest = {'case_id': 'research_network', 'source_sha256': hashlib.sha256(source).hexdigest(),
        'reference_sha256': hashlib.sha256(reference).hexdigest(), 'max_requests': args.max_requests,
        'comparison_policy_sha256': hashlib.sha256(policy_bytes).hexdigest(),
        'max_output_tokens': args.max_output_tokens, 'model_override': args.model,
        'automatic_retries': False, 'knowledge_capture': True, 'execution_scope': 'knowledge_capture_unit'}
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Testing knowledge capture directly; model calls can use credits. No planner, search or answer extraction.', flush=True)
    print('Experiment folder:', directory, flush=True)
    code = capture_fixture(directory, args)
    manifest['capture_exit_code'] = code
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    result = save_report(directory)
    return code or (0 if result['status'] == 'PASS' else 1)


def main(argv=None):
    """Dispatch free inspection, a focused capture test with the real model, or local re-scoring.

    Args:
        argv: Argument strings excluding the module name; None reads the process arguments.

    Returns:
        Shell exit code: 0 for inspection or a passing evaluation, 1 for graph differences
        or incomplete work, 2 for bad arguments/artifacts, or the component failure code.
        No provider access occurs outside the explicit run command.
    """
    parser = argparse.ArgumentParser(description='Known-source knowledge graph evaluation: 7 nodes, 8 edges, 2 attributes.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('show', help='Show the source and expected graph without model calls')
    run = commands.add_parser('run', help='Test the capture unit directly with the real model; uses credits, no planner')
    run.add_argument('--max-requests', type=int, default=2, help='Capture-call ceiling; default 2, normally one call for this source')
    run.add_argument('--max-output-tokens', type=int, default=8192)
    run.add_argument('--model')
    run.add_argument('--output-dir', type=Path, default=Path('.projectclarity/evaluations'))
    score = commands.add_parser('score', help='Re-score an experiment from its frozen source/key; no model calls')
    score.add_argument('experiment_dir', type=Path)
    score.add_argument('--policy', type=Path, help='Save a separate re-assessment under this policy; original result remains unchanged')
    args = parser.parse_args(argv)
    if args.command == 'run' and (not 2 <= args.max_requests <= 1000 or not 128 <= args.max_output_tokens <= 8192):
        parser.error('Use 2–1000 requests and 128–8192 output tokens; these are ceilings, not completion guarantees.')
    try:
        if args.command == 'show':
            print('Source document:', CASE_DIRECTORY / 'source.txt')
            print((CASE_DIRECTORY / 'source.txt').read_text())
            print('Expected graph:', CASE_DIRECTORY / 'expected_graph.json')
            reference = json.loads((CASE_DIRECTORY / 'expected_graph.json').read_text())
            print(f"Expected nodes: {len(reference['nodes'])}")
            print(f"Expected relationships: {len(reference['relationships'])}")
            print(f"Expected attributes: {len(reference['attributes'])}")
            print(json.dumps(reference, indent=2, ensure_ascii=False))
            return 0
        if args.command == 'run':
            return run_evaluation(args)
        return 0 if save_report(args.experiment_dir, policy_path=args.policy)['status'] == 'PASS' else 1
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        print('Graph evaluation input/artifact error:', str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
