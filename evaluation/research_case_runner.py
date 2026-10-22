"""Run one dated evaluation question through the normal ProjectClarity CLI.

`list` and `show` are free, offline preparation commands. `run CASE_ID` deliberately
starts real research and may spend model credits; it passes only the question and
source URLs, never reference answers. Each invocation gets its own artifact folder,
including a frozen reference copy, so later edits do not silently change a score.
`score CASE_ID RUN_FOLDER` only reads existing artifacts and makes no model calls.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid

from evaluation.membership_reference_cases import get_case, load_suite, reference_validation
from evaluation.membership_result_scoring import score_saved_run
from projectclarity.command_line import main as research_main


def write_json(path, value):
    """Save readable experiment metadata; no provider credentials are accepted here."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def print_score(result):
    """Separate source failures and unfinished work from reference-match counts."""
    print('Evaluation:', result['case_id'], '| reference date:', result['reference_date'])
    print('Research outcome:', result['run_status'])
    for key in ['anchored_relationships', 'supported_relationships']:
        metric = result[key]
        print(f"{key}: {metric['correct_count']}/{metric['expected_count']} reference relations; "
              f"{len(metric['unexpected'])} unexpected; precision={metric['precision']}, recall={metric['recall']}")
    print('All sources retrieved:', result['source_coverage_complete'])
    if check := result.get('acceptance'):
        print('Built-in multi-source test:', check['status'].upper())
        print(f"People matched: {len(check['matched_people'])}/{check['expected_people_count']} | "
              f"distinct people found: {check['observed_people_count']} | "
              f"contributing sources: {check['contributing_source_count']}")
        for label, key in [('Found', 'matched_people'), ('Missing', 'missing_people'),
                           ('Unexpected', 'unexpected_people'), ('Incomplete', 'incomplete_reasons')]:
            if check[key]:
                print(label + ':', ', '.join(check[key]))
    print_reference_validation(result)
    print('Semantic correctness/citation faithfulness still require review; this is not an overall quality pass.')


def print_reference_validation(suite_or_score):
    """Tell the reader who checked the answer key, without asking them to approve it."""
    validation = reference_validation(suite_or_score)
    print('Reference validation:', validation['status'], '| reviewer:', validation['reviewer'],
          '| checked:', validation.get('verified_on', 'not recorded'))


def run_case(suite, case, args):
    """Save inputs, call the real CLI once, and score its saved output even when partial.

    No automatic repeat is made after a budget stop or failed model call. Source
    access errors remain source access errors. Each case has its own explicit
    request cap; no hidden batch runner multiplies the requested spend.
    """
    directory = args.output_dir / case['case_id'] / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    frozen = json.dumps(suite, ensure_ascii=False, sort_keys=True).encode()
    write_json(directory / 'reference_suite.json', suite)
    write_json(directory / 'experiment.json', {'case_id': case['case_id'],
        'reference_sha256': hashlib.sha256(frozen).hexdigest(),
        'max_requests': args.max_requests, 'max_output_tokens': args.max_output_tokens,
        'source_mode': 'live_urls', 'automatic_retries': False})
    urls = directory / 'sources.txt'
    urls.write_text('\n'.join(s['url'] for s in case['sources']) + '\n', encoding='utf-8')
    argv = ['research', '--question', case['question'], '--urls', str(urls),
        '--extract', '--review-claims', '--non-interactive', '--max-requests', str(args.max_requests),
        '--max-output-tokens', str(args.max_output_tokens), '--output-dir', str(directory / 'runs')]
    if args.show_prompts:
        argv.append('--show-prompts')
    print('Starting one live evaluation case; model calls may spend credits.', flush=True)
    print('Experiment folder:', directory, flush=True)
    code = research_main(argv)
    sessions = list((directory / 'runs').glob('*/session.json'))
    if len(sessions) != 1:
        print('No unique saved session to score. Inspect the research error; no model retry was made.')
        return code or 1
    result = score_saved_run(suite, case, sessions[0].parent)
    result['research_exit_code'] = code
    write_json(directory / 'score.json', result)
    print_score(result)
    print('Score and inspectable differences:', directory / 'score.json')
    # Only explicit acceptance cases are executable tests. Older exploratory
    # cases retain their research exit code and do not gain an accuracy gate.
    return code or acceptance_exit_code(result)


def acceptance_exit_code(result):
    """Return nonzero for a failed/incomplete explicit test, so automation can catch it."""
    check = result.get('acceptance')
    return 1 if check and check['status'] != 'passed' else 0


def main(argv=None):
    """Dispatch explicit offline inspection or a single opt-in live research experiment."""
    parser = argparse.ArgumentParser(description='Prepare, run or score small dated legislature membership evaluations.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list', help='List prepared cases; no network or model calls')
    show = commands.add_parser('show', help='Show question, source URLs and reference answers; no calls')
    show.add_argument('case_id')
    run = commands.add_parser('run', help='Run one case through ProjectClarity; can spend model credits')
    run.add_argument('case_id')
    run.add_argument('--max-requests', type=int, default=20)
    run.add_argument('--max-output-tokens', type=int, default=4096)
    run.add_argument('--show-prompts', action='store_true')
    run.add_argument('--output-dir', type=Path, default=Path('.projectclarity/evaluations'))
    score = commands.add_parser('score', help='Score an existing case run without model calls')
    score.add_argument('case_id')
    score.add_argument('run_folder', type=Path)
    score.add_argument('--reference-suite', type=Path, help='Use the frozen reference_suite.json from an earlier experiment')
    args = parser.parse_args(argv)
    if args.command == 'run' and (not 3 <= args.max_requests <= 30 or not 128 <= args.max_output_tokens <= 8192):
        parser.error('Review requires 3–30 model requests and 128–8192 output tokens per request.')
    try:
        suite = (json.loads(args.reference_suite.read_text(encoding='utf-8'))
                 if args.command == 'score' and args.reference_suite else load_suite())
        if args.command == 'list':
            print('Reference date:', suite['reference_date'])
            print_reference_validation(suite)
            for case in suite['cases']:
                print(case['case_id'], '—', case['title'], f"({len(case['expected_members'])} expected members)")
            return 0
        case = get_case(suite, args.case_id)
        if args.command == 'show':
            print(json.dumps(case, ensure_ascii=False, indent=2))
            return 0
        if args.command == 'run':
            return run_case(suite, case, args)
        result = score_saved_run(suite, case, args.run_folder)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return acceptance_exit_code(result)
    except (ValueError, OSError, KeyError) as exc:
        print('Evaluation input/artifact error:', str(exc), file=sys.stderr)
        return 2
