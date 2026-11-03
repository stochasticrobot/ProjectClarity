"""Run repeatable research-step experiments and help a person judge their results.

A working application can still miss important facts or select irrelevant ones.
This tool runs requirements definition, extraction and relevance selection against
a fixed collection of documents and questions. Keeping the inputs fixed makes it
possible to compare results after a change without also changing the test material.

It saves the results and prepares a review worksheet. A person supplies judgments
about meaning and completeness; the tool calculates scores from those judgments
and checks that the underlying saved results have not changed. Its answer key is
for evaluation, not model input. The default run is a no-call preview, and a valid
response format is never presented as proof of accurate research.
"""

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import projectclarity.evidence_selection.relevance_selection as relevance_selection
from testing import extraction_test_rig
from projectclarity.knowledge_extraction.knowledge_extraction import save

ROOT = Path(__file__).resolve().parents[1]
PACKET = ROOT / 'testing/benchmark_documents/research_packet_v1'


def read_json(path):
    """Read a saved JSON artifact for orchestration or assessment.

    Args:
        path: Path to a UTF-8 JSON file.
    Returns:
        Decoded content; missing files and malformed JSON propagate as errors.
    """
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    """Hash exact artifact bytes to bind reviews and preserve experiment identity.

    Args:
        path: Existing file to hash.
    Returns:
        SHA-256 hexadecimal digest; read failures propagate without mutation.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def new_folder(parent, prefix):
    """Create an isolated private directory without reusing old results.

    Args:
        parent: Parent output directory.
        prefix: Descriptive experiment or assessment prefix.
    Returns:
        New directory path. Creation errors propagate; existing data is untouched.
    """
    folder = parent / (prefix + datetime.now(timezone.utc).strftime('-%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(parents=True, mode=0o700)
    return folder


def packet_data():
    """Validate the fixed packet and load its separate evaluator-only answer key.

    Args:
        None.
    Returns:
        Questions, ordered chunk paths, and answer key. Rejects missing evidence,
        duplicate IDs and invalid relevance labels before any model call.
    """
    questions = read_json(PACKET / 'questions.json')
    chunks = sorted((PACKET / 'chunks').glob('*.txt'))
    gold = read_json(PACKET / 'answer_key.json')
    assert len(questions) == 3 and len(chunks) == 6, 'Expected three questions and six chunks'
    ids = [a['id'] for a in gold['assertions']]
    assert len(ids) == len(set(ids)), 'Duplicate assertion IDs'
    for assertion in gold['assertions']:
        assert assertion['quote'] in (PACKET / 'chunks' / assertion['chunk']).read_text()
        assert set(assertion['relevance']) == {q['id'] for q in questions}
        assert set(assertion['relevance'].values()) <= {'required', 'optional', 'irrelevant'}
    return questions, chunks, gold


def invoke(module, argv, output):
    """Invoke an existing CLI entry point and locate its isolated receipt directory.

    Args:
        module: Existing extraction_test_rig or relevance_selection module, not a replacement path.
        argv: CLI arguments without output-root.
        output: Unique parent directory reserved for this one invocation.
    Returns:
        Exit status and receipt folder, or an error receipt for CLI preflight errors.
        Module calls may access the provider. Unexpected exceptions propagate.
    """
    output.mkdir(parents=True, mode=0o700)
    try:
        code = module.main([*map(str, argv), '--output-root', str(output)])
    except SystemExit as error:
        code = int(error.code or 0)
    children = list(output.iterdir())
    if len(children) == 1 and children[0].is_dir():
        return code, children[0]
    failure = output / 'preflight-failure'
    failure.mkdir(mode=0o700)
    save(failure / 'summary.json', {'status': 'failed', 'attempted_calls': 0,
                                  'error': 'CLI failed before producing receipts; see terminal diagnostics'}, '')
    return code or 1, failure


def run_benchmark(args):
    """Run or preview bounded end-to-end trials with frozen inputs and provenance.

    Args:
        args: Parsed run options: repeat count, call cap, model, env file, live flag,
            and output root. Live opts into up to 39 provider calls per repetition.
    Returns:
        Zero for preview or completed structural checks, one for incomplete trials.
        Live mode writes receipts, a blank semantic review, and initial report.
        No retries, answer generation, cross-chunk merging or automatic grading.
    """
    questions, chunks, gold = packet_data()
    maximum = args.repeat * len(questions) * (1 + 2 * len(chunks))
    if args.repeat < 1 or maximum > args.max_calls:
        raise ValueError(f'Requires at most {maximum} calls; repeat must be positive and within --max-calls')
    if not args.live:
        print(json.dumps({'mode': 'preview', 'maximum_calls': maximum, 'repetitions': args.repeat,
                          'questions': [q['question'] for q in questions],
                          'chunks': [p.name for p in chunks], 'semantic_scores': 'require human review'}, indent=2))
        return 0
    folder = new_folder(args.output_root, 'benchmark')
    snapshot = folder / 'packet'
    snapshot.mkdir(mode=0o700)
    for source in [*chunks, PACKET / 'questions.json', PACKET / 'answer_key.json', PACKET / 'RUBRIC.md']:
        target = snapshot / source.name
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
    frozen_chunks = [snapshot / p.name for p in chunks]
    tracked = [*ROOT.glob('src/projectclarity/prompts/*.txt'), ROOT / 'testing/research_benchmark.py', ROOT / 'testing/extraction_test_rig.py',
               ROOT / 'src/projectclarity/knowledge_extraction/knowledge_extraction.py', ROOT / 'src/projectclarity/question_definition/requirements_definition.py', ROOT / 'src/projectclarity/evidence_selection/relevance_selection.py', ROOT / 'requirements.txt']
    code_hashes = {str(p.relative_to(ROOT)): digest(p) for p in tracked}
    save(folder / 'manifest.json', {'maximum_calls': maximum, 'model': args.model,
         'repeat': args.repeat, 'code_and_prompt_hashes': code_hashes,
         'packet_hashes': {p.name: digest(p) for p in snapshot.iterdir()},
         'policy': 'research-packet-v1/manual-semantic-review-v1',
         'scope': 'Three stages to per-chunk Relevant Knowledge stores, not final answers'}, '')
    trials = []
    common = ['--model', args.model, '--env-file', args.env_file]
    for repeat in range(1, args.repeat + 1):
        for question in questions:
            trial_id = f"r{repeat}-{question['id']}"
            trial_root = folder / trial_id
            _, rig = invoke(extraction_test_rig, ['combined', *frozen_chunks, '--question', question['question'],
                            '--max-calls', str(1 + len(chunks)), *common], trial_root / 'upstream')
            trial = {'id': trial_id, 'question_id': question['id'],
                     'rig': str(rig.relative_to(folder)), 'chunks': []}
            for index, source in enumerate(chunks):
                extraction = rig / f'chunk-{index:04d}.json'
                entry = {'chunk': source.name, 'extraction': None, 'selection': None}
                if extraction.exists():
                    entry['extraction'] = str(extraction.relative_to(folder))
                    if read_json(extraction)['status'] == 'checks_pass':
                        _, selected = invoke(relevance_selection, [extraction, '--question', question['question'],
                               '--requirements', rig / 'requirements.json', *common], trial_root / f'select-{index:04d}')
                        entry['selection'] = str(selected.relative_to(folder))
                trial['chunks'].append(entry)
            trials.append(trial)
            # Checkpoints retain completed trials if a later process is interrupted.
            save(trial_root / 'trial.json', trial, '')
    unchanged = all(digest(ROOT / name) == value for name, value in code_hashes.items())
    save(folder / 'trials.json', {'trials': trials, 'code_and_prompts_unchanged': unchanged}, '')
    # Bind later semantic reviews to exact saved receipts, sources and benchmark policy.
    hashes = {str(p.relative_to(folder)): digest(p) for p in folder.rglob('*') if p.is_file()}
    save(folder / 'evidence_hashes.json', hashes, '')
    write_workbook(folder)
    review = review_template(folder)
    save(folder / 'review.json', review, '')
    result = assess(folder, folder / 'review.json')
    print(f'Benchmark: {folder.resolve()}\nSemantic review: {folder / "review.json"}', flush=True)
    return int(result['workflow_status'] != 'checks_pass')


def trial_records(folder, trial):
    """Reconstruct fact identities and check selected stores against original records.

    Args:
        folder: Frozen benchmark directory.
        trial: Recorded paths for one question and repetition.
    Returns:
        Facts keyed by stable ID, selected IDs, chunk states, and integrity status.
        Failed stages remain explicit; malformed artifacts raise rather than pass.
    """
    facts, selected, states = {}, set(), {}
    integrity = True
    for entry in trial['chunks']:
        name = entry['chunk']
        states[name] = 'extraction_unavailable'
        if not entry['extraction']:
            continue
        path = folder / entry['extraction']
        receipt = read_json(path)
        if receipt['status'] != 'checks_pass':
            continue
        knowledge = relevance_selection.prepare_knowledge(receipt, digest(path))
        facts.update({f['fact_id']: {'chunk': name, **f} for f in knowledge['facts']})
        states[name] = 'selection_unavailable'
        if not entry['selection']:
            continue
        selection_folder = folder / entry['selection']
        store_path = selection_folder / 'relevant_knowledge.json'
        if not store_path.exists():
            continue
        selection_receipt = read_json(selection_folder / 'selection.json')
        selection = relevance_selection.validate_selection(json.dumps(selection_receipt['parsed']), knowledge)
        expected = relevance_selection.relevant_knowledge(selection, knowledge)
        actual = read_json(store_path)
        integrity &= all(actual.get(k) == v for k, v in expected.items())
        requirements = read_json(folder / trial['rig'] / 'requirements.json')['information_needed']
        question = next(q for q in read_json(folder / 'packet/questions.json') if q['id'] == trial['question_id'])
        integrity &= (actual['source_artifact']['sha256'] == digest(path)
                      and actual['research_question'] == question['question']
                      and actual['requested_information'] == requirements)
        selected.update(f['fact_id'] for f in actual['facts'])
        states[name] = 'checks_pass' if selection_receipt['status'] == 'checks_pass' else 'selection_unavailable'
    return facts, selected, states, bool(integrity)


def review_template(folder):
    """Create explicit unanswered judgments without guessing semantic correctness.

    Args:
        folder: Completed benchmark folder with trials and frozen answer key.
    Returns:
        Review object binding fact judgments, source-assertion support bundles, and
        planner criteria to the immutable run. Nulls mean pending, never failure.
    """
    gold = read_json(folder / 'packet/answer_key.json')['assertions']
    questions = {q['id']: q for q in read_json(folder / 'packet/questions.json')}
    trials = []
    for trial in read_json(folder / 'trials.json')['trials']:
        facts, selected, states, integrity = trial_records(folder, trial)
        trials.append({'id': trial['id'],
          'requirements': {criterion: None for criterion in questions[trial['question_id']]['criteria']},
          'facts': {fid: {'meaning_correct': None, 'evidence_complete': None,
                         'relevance': None, 'notes': ''} for fid in facts},
          'assertions': {a['id']: {'supporting_fact_ids': None, 'notes': ''} for a in gold}})
    return {'evidence_sha256': digest(folder / 'evidence_hashes.json'), 'reviewer': '', 'trials': trials}


def write_workbook(folder):
    """Present source, planner and extracted records together for semantic review.

    Args:
        folder: Completed run containing frozen packet and all trial receipts.
    Returns:
        None. Writes an owner-only review-workbook.md with actual records and IDs;
        no model calls or semantic judgments are made. File errors propagate.
    """
    lines = ['# Semantic review workbook', '',
             'Use packet/RUBRIC.md and edit review.json. Null means pending.', '']
    for trial in read_json(folder / 'trials.json')['trials']:
        lines.extend([f"## {trial['id']}", ''])
        plan = folder / trial['rig'] / 'plan.json'
        lines.extend(['### Planner output', '', '```json',
                      json.dumps(read_json(plan).get('parsed') if plan.exists() else None, indent=2), '```', ''])
        facts, selected, states, integrity = trial_records(folder, trial)
        for chunk, state in states.items():
            lines.extend([f'### {chunk}: {state}', '', 'Source:', '',
                          (folder / 'packet' / chunk).read_text(), ''])
            for fid, item in facts.items():
                if item['chunk'] == chunk:
                    lines.extend([f"Fact `{fid}` — selected: {fid in selected}", '', '```json',
                                  json.dumps(item['fact'], ensure_ascii=False, indent=2), '```', ''])
    path = folder / 'review-workbook.md'
    path.write_text('\n'.join(lines))
    path.chmod(0o600)


def ratio(numerator, denominator):
    """Represent a measured fraction without turning an empty denominator into a pass.

    Args:
        numerator: Observed successful units.
        denominator: Observed eligible units.
    Returns:
        Counts and a fraction, or null fraction when no units are eligible.
    """
    return {'matched': numerator, 'total': denominator,
            'fraction': numerator / denominator if denominator else None}


def assess(folder, review_path):
    """Score reviewed semantics and immutable workflow evidence in a new assessment.

    Args:
        folder: Existing benchmark run; hashed evidence must remain unchanged.
        review_path: Human-edited review JSON with explicit semantic judgments.
    Returns:
        Summary with per-trial scores and assertion loss ledger. Writes new JSON
        and Markdown assessment files, preserving prior reports. Rejects altered
        evidence, invalid mappings and incomplete review shapes. Null judgments
        leave semantic scores pending; failed workflows are always incomplete.
    """
    hashes = read_json(folder / 'evidence_hashes.json')
    for name, value in hashes.items():
        if name != 'credential_redaction_applied' and digest(folder / name) != value:
            raise ValueError(f'Frozen evidence changed: {name}')
    review = read_json(review_path)
    if review['evidence_sha256'] != digest(folder / 'evidence_hashes.json'):
        raise ValueError('Review belongs to different evidence')
    template = review_template(folder)
    reviews = {r['id']: r for r in review['trials']}
    if len(reviews) != len(review['trials']) or set(reviews) != {t['id'] for t in template['trials']}:
        raise ValueError('Review trial IDs must match exactly')
    gold = read_json(folder / 'packet/answer_key.json')['assertions']
    trial_data = read_json(folder / 'trials.json')
    reports, attempted = [], 0
    for trial, blank in zip(trial_data['trials'], template['trials']):
        judged = reviews[trial['id']]
        for field in ('facts', 'assertions', 'requirements'):
            if set(judged[field]) != set(blank[field]):
                raise ValueError(f'Review {field} keys do not match {trial["id"]}')
        facts, selected, states, integrity = trial_records(folder, trial)
        upstream = read_json(folder / trial['rig'] / 'summary.json')
        attempted += upstream['attempted_calls']
        for entry in trial['chunks']:
            if entry['selection']:
                attempted += read_json(folder / entry['selection'] / 'summary.json')['attempted_calls']
        complete = all(s == 'checks_pass' for s in states.values()) and upstream['status'] == 'checks_pass'
        pending = False
        for value in judged['requirements'].values():
            if value is not None and type(value) is not bool:
                raise ValueError('Requirement judgments must be true, false or null')
            pending |= value is None
        for value in judged['facts'].values():
            for key in ('meaning_correct', 'evidence_complete'):
                if value[key] is not None and type(value[key]) is not bool:
                    raise ValueError('Fact judgments must be true, false or null')
                pending |= value[key] is None
            if value['relevance'] not in (None, 'required', 'optional', 'irrelevant'):
                raise ValueError('Invalid fact relevance judgment')
            pending |= value['relevance'] is None
        captured, retained, required, ledger = set(), set(), set(), []
        for assertion in gold:
            aid = assertion['id']
            support = judged['assertions'][aid]['supporting_fact_ids']
            if support is None:
                pending = True
                status = 'pending_review'
            else:
                if not isinstance(support, list) or len(set(support)) != len(support):
                    raise ValueError('Support must be a unique list of fact IDs, [] for missing, or null')
                if any(fid not in facts or facts[fid]['chunk'] != assertion['chunk'] for fid in support):
                    raise ValueError('Support references an unknown fact or another chunk')
                valid = bool(support) and all(judged['facts'][fid]['meaning_correct'] is True
                         and judged['facts'][fid]['evidence_complete'] is True for fid in support)
                if valid:
                    captured.add(aid)
                    if set(support) <= selected:
                        retained.add(aid)
                unresolved = any(judged['facts'][fid][key] is None for fid in support
                                 for key in ('meaning_correct', 'evidence_complete'))
                status = ('pending_review' if unresolved else 'retained' if aid in retained
                          else 'selection_missed' if valid else 'extraction_missed' if not support
                          else 'extraction_invalid')
            if states[assertion['chunk']] == 'extraction_unavailable':
                status = 'extraction_unavailable'
            elif status == 'selection_missed' and states[assertion['chunk']] == 'selection_unavailable':
                status = 'selection_unavailable'
            label = assertion['relevance'][trial['question_id']]
            if label == 'required':
                required.add(aid)
            ledger.append({'assertion_id': aid, 'relevance': label, 'status': status,
                           'supporting_fact_ids': support})
        if not pending and not review.get('reviewer', '').strip():
            raise ValueError('Completed semantic assessment requires reviewer identity')
        relevant = {fid for fid, v in judged['facts'].items() if v['relevance'] in ('required', 'optional')}
        valid_facts = {fid for fid, v in judged['facts'].items()
                       if v['meaning_correct'] is True and v['evidence_complete'] is True}
        scores = None if pending else {
          'requirements_coverage': ratio(sum(judged['requirements'].values()), len(judged['requirements'])),
          'extraction_coverage': ratio(len(captured), len(gold)),
          'meaning_accuracy': ratio(sum(v['meaning_correct'] for v in judged['facts'].values()), len(facts)),
          'complete_evidence': ratio(sum(v['evidence_complete'] for v in judged['facts'].values()), len(facts)),
          'selection_recall': ratio(len(selected & relevant), len(relevant)),
          'selection_precision': ratio(len(selected & relevant), len(selected)),
          'selected_valid_relevance': ratio(len(selected & relevant & valid_facts), len(selected)),
          'end_to_end_recall': ratio(len(retained & required), len(required))}
        reports.append({'id': trial['id'], 'question_id': trial['question_id'], 'workflow_status': 'checks_pass' if complete and integrity else 'incomplete',
          'semantic_status': 'pending_review' if pending else 'reviewed', 'scores': scores,
          'scores_scope': 'complete trial' if complete else 'diagnostic only: incomplete workflow',
          'record_integrity': integrity if complete else None, 'chunks': states,
          'required_assertions': len(required), 'extracted_facts': len(facts), 'selected_facts': len(selected),
          'assertion_ledger': ledger})
    summary = {'workflow_status': 'checks_pass' if trial_data['code_and_prompts_unchanged'] and
               all(r['workflow_status'] == 'checks_pass' for r in reports) else 'incomplete',
               'attempted_calls': attempted, 'semantic_status': 'pending_review' if
               any(r['semantic_status'] == 'pending_review' for r in reports) else 'reviewed',
               'trials': reports, 'scope': 'Frozen synthetic packet; no held-out or live-web accuracy claim'}
    comparisons = {}
    for question_id in sorted({r['question_id'] for r in reports}):
        group = [r for r in reports if r['question_id'] == question_id]
        eligible = [r for r in group if r['workflow_status'] == 'checks_pass' and r['scores'] is not None]
        ranges = {}
        if eligible:
            for metric in eligible[0]['scores']:
                values = [r['scores'][metric]['fraction'] for r in eligible
                          if r['scores'][metric]['fraction'] is not None]
                ranges[metric] = {'observed_repetitions': len(values),
                                  'min': min(values) if values else None,
                                  'max': max(values) if values else None}
        comparisons[question_id] = {'trials': len(group), 'reviewed_complete_trials': len(eligible),
                                    'metric_ranges': ranges}
    summary['repeat_comparison'] = comparisons
    output = new_folder(folder, 'assessment')
    save(output / 'review_snapshot.json', review, '')
    save(output / 'scores.json', summary, '')
    lines = ['# End-to-end benchmark', '', f"Workflow: **{summary['workflow_status']}**. "
             f"Semantic assessment: **{summary['semantic_status']}**. Calls attempted: {attempted}.", '',
             'Structural success is not semantic accuracy. Incomplete trials are not successful empty results.', '',
             '[Review workbook](../review-workbook.md) · [Frozen rubric](../packet/RUBRIC.md)', '']
    for report in reports:
        lines.extend([f"## {report['id']}", '', f"Extracted: {report['extracted_facts']}; selected: {report['selected_facts']}; "
                      f"record integrity: {report['record_integrity']}. {report['scores_scope']}.", ''])
        if report['scores'] is not None:
            for name, metric in report['scores'].items():
                lines.append(f"- {name}: {metric['matched']}/{metric['total']} "
                             f"({metric['fraction']:.1%})" if metric['fraction'] is not None else f'- {name}: N/A (0 denominator)')
        else:
            lines.append('Semantic scores pending: complete review.json using packet/RUBRIC.md.')
        lines.extend(['', '| Assertion | Relevance | Outcome |', '|---|---|---|'])
        lines.extend(f"| {row['assertion_id']} | {row['relevance']} | {row['status']} |" for row in report['assertion_ledger'])
        lines.append('')
    lines.extend(['## Repetition comparison', '',
                  'Ranges use only reviewed, complete trials; pending and failed trials remain counted separately.', '',
                  '```json', json.dumps(comparisons, indent=2), '```', ''])
    report_path = output / 'report.md'
    report_path.write_text('\n'.join(lines) + '\n')
    report_path.chmod(0o600)
    print(f'Assessment: {output.resolve()}', flush=True)
    return summary


def main(argv=None):
    """Expose a preview/live runner and repeatable assessment of existing receipts.

    Args:
        argv: Optional CLI arguments; None reads process arguments.
    Returns:
        Zero for preview or successful execution, one for incomplete workflow.
        Input errors become CLI errors. Assess never contacts a model provider.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    run.add_argument('--live', action='store_true')
    run.add_argument('--repeat', type=int, default=1)
    run.add_argument('--max-calls', type=int, default=39)
    run.add_argument('--model', default=extraction_test_rig.DEFAULT_MODEL)
    run.add_argument('--env-file', type=Path, default=ROOT / '.env')
    run.add_argument('--output-root', type=Path, default=ROOT / 'runs')
    score = commands.add_parser('assess')
    score.add_argument('run', type=Path)
    score.add_argument('--review', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'run':
            return run_benchmark(args)
        summary = assess(args.run, args.review)
        return int(summary['workflow_status'] != 'checks_pass')
    except (ValueError, OSError, AssertionError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
