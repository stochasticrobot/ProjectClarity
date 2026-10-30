"""Run or score extraction from one frozen HTML page without the full research workflow.

The run and resume commands use the application's document parser, passage
extractor, quotation validator and candidate storage. They can spend model credits,
but do not invoke planning, search, link exploration, coverage assessment or claim
review. The score command reads a separate expected-answer file and saved results
without a model call. This isolates passage extraction from the other causes of
missing final report entries; it does not establish full-workflow performance.
"""
import argparse
from datetime import datetime, UTC
import hashlib
import json
from pathlib import Path
import uuid

from langsmith import tracing_context

from evaluation.page_extraction_scoring import score_page
from projectclarity.nodes.extract_knowledge.node import extract_knowledge
from projectclarity.nodes.validate_evidence.node import validate_evidence
from projectclarity.nodes.persist_candidates.node import persist_candidates
from projectclarity.research_schemas import (
    Budget, FileSource, ResearchPlan, ResearchRequest, ResearchSession, RetrievedDocument, Usage,
)
from projectclarity.retrieval.document_text_extractor import parse_document
from projectclarity.retrieval.retrieval_limits import RetrievalLimits
from projectclarity.session_storage import save_session, write_text


def prepare(source, question, output_root, max_requests, model=None):
    """Freeze one HTML page and supply the minimal task data needed by extraction.

    Args:
        source: Local HTML path, not a URL to fetch.
        question: Operator-supplied research question; never loaded from an answer key.
        output_root: Directory under which a fresh isolated run is created.
        max_requests: Whole-experiment model-attempt ceiling including retries.
        model: Optional configured provider model name; None keeps the application default.

    Returns:
        Saved ResearchSession with production-parsed text and a fixed one-goal plan.
        No model call is made. Parse/read errors propagate; no expected answers are read.
    """
    raw = source.read_bytes()
    text, parser = parse_document(raw, 'text/html; charset=UTF-8', RetrievalLimits())
    now = datetime.now(UTC).isoformat()
    request_args = {'question': question, 'plan_only': False, 'extract_knowledge': True,
                    'budget': Budget(max_requests=max_requests, max_output_tokens=8192)}
    if model:
        request_args['model'] = model
    request = ResearchRequest(**request_args)
    plan = ResearchPlan(objective=question, question_category='extraction benchmark',
        scope_in=[question], scope_out=[], assumptions=[], clarifications=[],
        goals=[{'goal_id': 'G1', 'question': question,
                'evidence_required': ['Source text supporting the requested answer.']}],
        strategy=[{'action': 'Extract the supplied page.', 'goal_ids': ['G1'],
                   'source_ids': ['S1'], 'proposed_searches': []}])
    run_id = uuid.uuid4().hex
    directory = output_root / run_id
    saved = directory / 'documents/S1'
    saved.mkdir(parents=True, exist_ok=False)
    (saved / 'raw.bin').write_bytes(raw)
    (saved / 'text.txt').write_text(text, encoding='utf-8')
    session = ResearchSession(session_id=run_id, created_at=now, request=request,
        mode='plan_retrieve_extract', status='sources_retrieved', plan=plan,
        sources=[FileSource(source_id='S1', path=str(source.resolve()))],
        usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0),
        documents=[RetrievedDocument(source_id='S1', source_kind='file',
            original_location=str(source.resolve()), status='retrieved', attempted_at=now,
            acquired_at=now, content_type='text/html', raw_sha256=hashlib.sha256(raw).hexdigest(),
            text_sha256=hashlib.sha256(text.encode()).hexdigest(), byte_count=len(raw),
            character_count=len(text), raw_path='documents/S1/raw.bin',
            text_path='documents/S1/text.txt', parser=parser)])
    save_session(session, output_root)
    write_text(directory / 'benchmark.json', json.dumps({
        'scope': 'Single-page extraction, evidence validation and candidate persistence only.',
        'transport': 'live configured provider', 'expected_answers_supplied_to_model': False,
        'parser': parser, 'source_sha256': session.documents[0].raw_sha256,
    }, indent=2) + '\n')
    return session


def execute(session, root, max_batches=None):
    """Invoke only production extraction and local validation/persistence.

    Args:
        session: Prepared or previously saved isolated extraction session.
        root: Parent directory of that session's saved run.
        max_batches: Optional scheduling quantum; None processes all eligible passages.

    Returns:
        Updated session with calls, replies, evidence and unfinished work saved. A completed
        passage does not establish exhaustive fact extraction. Remote tracing is disabled.
    """
    with tracing_context(enabled=False):
        session = extract_knowledge({'session': session}, root, max_batches=max_batches)['session']
        session = validate_evidence({'session': session}, root)['session']
        session = persist_candidates({'session': session}, root)['session']
    return session


def main(argv=None):
    """Expose explicit live-run, resume and read-only scoring commands.

    Args:
        argv: Optional argument list; None uses the process command line.

    Returns:
        Zero for a completed extraction invocation or perfect complete anchored score;
        one for incomplete/mismatched work. Invalid inputs raise with local diagnostics.
        A zero run exit is operational completion, never an accuracy claim.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run', help='Invoke the live model on one saved HTML page')
    run.add_argument('--source', type=Path, required=True)
    run.add_argument('--question', required=True)
    run.add_argument('--output-root', type=Path, default=Path('.projectclarity/extraction_benchmarks'))
    run.add_argument('--max-requests', type=int, default=150)
    run.add_argument('--max-batches', type=int)
    run.add_argument('--model')
    resume = sub.add_parser('resume', help='Continue unattempted passages without replaying failed calls')
    resume.add_argument('directory', type=Path)
    resume.add_argument('--max-batches', type=int)
    score = sub.add_parser('score', help='Compare saved relationships; never call a model')
    score.add_argument('directory', type=Path)
    score.add_argument('--reference', type=Path, required=True)
    score.add_argument('--recover-local-ids', action='store_true',
                       help='Separately score deterministic ID recovery of failed replies; do not alter originals')
    score.add_argument('--source-equivalences', action='store_true',
                       help='Separate comparison using source-province aliases and explicit historical exclusions; retain strict score')
    args = parser.parse_args(argv)
    if args.command == 'score':
        result = score_page(args.directory, args.reference, recover_ids=args.recover_local_ids,
                            source_equivalences=args.source_equivalences)
        name = 'score' + ('-id-recovery' if args.recover_local_ids else '') + ('-source-equivalences' if args.source_equivalences else '') + '.json'
        write_text(args.directory / name, json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        metrics = result['anchored_pairs']
        print(json.dumps({k: metrics[k] for k in ('expected_count', 'predicted_count', 'correct_count', 'precision', 'recall')}, indent=2))
        print(args.directory / name)
        return int(metrics['f1'] != 1 or bool(result['failed_batches'] or result['unfinished_batches']))
    if args.max_batches is not None and args.max_batches < 0:
        parser.error('--max-batches cannot be negative')
    if args.command == 'run':
        session = prepare(args.source, args.question, args.output_root, args.max_requests, args.model)
        root = args.output_root
    else:
        if not (args.directory / 'benchmark.json').is_file():
            parser.error('Resume accepts isolated benchmark runs, not full research runs.')
        session = ResearchSession.model_validate_json((args.directory / 'session.json').read_text())
        root = args.directory.parent
    print('Run:', root / session.session_id, flush=True)
    session = execute(session, root, args.max_batches)
    attempts = session.extraction['attempts']
    print('Completed passages:', sum(a['status'] == 'completed' for a in attempts), '/', len(attempts))
    print('Model attempts:', session.usage.requests_attempted)
    return int(any(a['status'] != 'completed' for a in attempts))


if __name__ == '__main__':
    raise SystemExit(main())
