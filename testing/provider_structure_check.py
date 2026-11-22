"""Check model output shapes through the production extraction worker.

The supplied documents are short synthetic fixtures, not a research-quality
benchmark. Live mode uses the real provider, budgets, validators, bounded repair
and missing-entity review. All replies are retained privately. Preview mode sends
nothing. Development and held-out cases are separate so tuning cannot silently
replace a failed held-out result with a later successful sample.
"""
import argparse
import hashlib
import json
from pathlib import Path
from queue import Queue
from threading import Event
from uuid import uuid4

from projectclarity.knowledge_extraction.concurrent_extraction import extract_chunk
from projectclarity.knowledge_extraction.knowledge_extraction import save, validate_response
from projectclarity.model_access.model_client import ModelCalls
from projectclarity.model_access.model_ledger import read_ledger

CASES = {
    'development': [('acme-original', 'Acme Labs is based in Ottawa. Mira Chen founded Acme Labs in 2020.')],
    'heldout': [
        ('laboratory', 'Cedar Robotics opened a laboratory in Halifax in 2022. Priya Shah directs the laboratory.'),
        ('negation', 'Mistral Foundation awarded no grants in 2024. Its office is in Lyon.'),
        ('literal-attributes', 'The vessel Aurora was launched in 1998. Its registered capacity is 24 passengers.'),
    ],
}


def verify_case(root, name, source_text, env_file, provider, model):
    """Run one immutable fixture through the real production extraction owner.

    Args:
        root: Private output directory; a new UUID child is created for this case.
        name: Synthetic fixture label, never a filename supplied by model output.
        source_text: Entire exact document text; no prefixing, slicing or truncation.
        env_file: Private provider credential/configuration path.
        provider: Registered backend override.
        model: Optional provider model override, or None for its configured default.
    Returns:
        Safe summary of status, accepted facts, call outcomes and conversion counts.
        Saves owner-only source, full worker result and model receipts. Makes up to
        three model calls in the existing worker (extraction, rejected-fact repair,
        omission review); no harness retry. A four-call/500000-token session budget
        provides headroom for those calls. Provider errors remain failed/partial
        evidence, not successful empty output. Filesystem errors propagate.
    """
    sid = str(uuid4()); folder = root / sid; folder.mkdir(mode=0o700)
    save(folder / 'session.json', {'model_request_budget': 4, 'model_token_budget': 500000}, '')
    save(folder / 'fixture.json', {'name': name, 'text': source_text}, '')
    caller = ModelCalls(root, env_file, provider=provider, model=model)
    source = {'id': 1, 'final_url': f'https://example.invalid/{name}',
              'text_sha256': hashlib.sha256(source_text.encode()).hexdigest()}
    chunk = {'start': 0, 'end': len(source_text), 'index': 0}
    result = extract_chunk(source, chunk, source_text, [], caller, sid, Event(), Queue())
    save(folder / 'chunk-result.json', result, '')
    parsed = result.get('parsed', {})
    accepted = False
    try:
        validate_response(json.dumps(parsed), source_text)
        accepted = result['status'] == 'checks_pass' and bool(parsed.get('facts'))
    except ValueError:
        pass
    ledger = read_ledger(folder)
    summary = {'case': name, 'provider': caller.provider, 'model': caller.model,
               'status': result['status'], 'passed_structural_check': accepted,
               'accepted_facts': len(parsed.get('facts', [])), 'artifact_directory': str(folder),
               'initial_rejections': len(result.get('initial_rejections', [])),
               'entity_issues': len(result.get('entity_issues', [])),
               'unresolved': len(result.get('unresolved', [])),
               'calls': [{key: row.get(key) for key in (
                   'stage', 'outcome', 'validation', 'response_format', 'structure_status',
                   'conversion_count', 'usage')} for row in ledger['calls']]}
    save(folder / 'summary.json', summary, '')
    return summary


def main(argv=None):
    """Preview or explicitly run a bounded production-path structure check.

    Args: argv: Optional CLI arguments; None reads process arguments. --live is
        required for provider requests; --cases selects development or heldout.
        --env-file, --provider, --model and --output-root configure the run.
    Returns: Zero for preview or all checks passing; one for any failed/partial
        case. Live mode writes a new private manifest/results folder, records
        implementation hashes, and prints only safe summaries. No server restart,
        deployment, retry or historical-result overwrite occurs.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', choices=CASES, default='development')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--env-file', type=Path, default=Path.cwd() / '.env')
    parser.add_argument('--provider', default='bedrock')
    parser.add_argument('--model')
    parser.add_argument('--output-root', type=Path, default=Path.cwd() / 'runs/provider-structures')
    args = parser.parse_args(argv)
    selected = CASES[args.cases]
    if not args.live:
        print(json.dumps({'cases': selected, 'maximum_generation_calls': 3 * len(selected), 'live': False}, indent=2))
        return 0
    root = args.output_root / (args.cases + '-' + uuid4().hex[:12])
    root.mkdir(parents=True, mode=0o700)
    package = Path(__file__).resolve().parents[1] / 'src/projectclarity'
    hashes = {str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted((package / 'model_access').rglob('*.py'))}
    save(root / 'manifest.json', {'case_group': args.cases, 'cases': selected,
         'implementation_sha256': hashes, 'scope': 'Synthetic structure checks, not semantic accuracy or full research reliability.'}, '')
    results = []
    for name, text in selected:
        result = verify_case(root, name, text, args.env_file, args.provider, args.model)
        results.append(result)
        print(json.dumps(result), flush=True)
    save(root / 'results.json', {'results': results}, '')
    print(f'Results: {root}', flush=True)
    return int(not all(result['passed_structural_check'] for result in results))


if __name__ == '__main__':
    raise SystemExit(main())
