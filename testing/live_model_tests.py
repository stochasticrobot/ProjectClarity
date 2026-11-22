"""Run three small real-model contract checks through the production caller.

Preview is offline by default. Explicit --live uses the configured backend and
model, without mocks, repair calls or fallback. These are smoke tests of output
and selected source facts, not a full research workflow or accuracy benchmark.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from projectclarity.knowledge_extraction.knowledge_extraction import (
    extraction_request, save, validate_response,
)
from projectclarity.model_access.model_client import ModelCalls
from projectclarity.model_access.providers.registry import resolve_config

SOURCE = 'Cedar Robotics is based in Halifax.'


class StrictRecord(BaseModel):
    """Require exact JSON types and reject unrequested fields."""
    model_config = ConfigDict(strict=True, extra='forbid')


class BasicOutput(StrictRecord):
    """Minimal typed answer to a small counting task."""
    count: int
    label: str
    present: bool


class Item(StrictRecord):
    """Named source item with an explicitly unknown optional property."""
    name: str
    colour: str | None


class NestedOutput(StrictRecord):
    """A list and nullable field testing unknown versus literal null text."""
    items: list[Item]
    note: str | None


CASES = ('basic-types', 'nested-null', 'source-extraction')


def build_request(case, model):
    """Build a small request with no provider-specific branches.

    Args:
        case: One of CASES; unknown names raise ValueError.
        model: Configured model ID passed unchanged to the production caller.
    Returns:
        Neutral request with a 768-token output cap and 60-second timeout.
        Extraction uses the existing application prompt/schema; other checks use
        small strict schemas. No network or filesystem writes occur.
    """
    if case == 'source-extraction':
        request = extraction_request(SOURCE, [], model, 768)
        request['timeout'] = 60
        return request
    if case == 'basic-types':
        schema = BasicOutput.model_json_schema()
        prompt = ('There are three red beads and two blue beads. Give the total '
                  'count, the label "beads", and whether any beads are present.')
    elif case == 'nested-null':
        schema = NestedOutput.model_json_schema()
        prompt = ('The inventory has one item named "null". Its colour is unknown. '
                  'There is no note. Return the item and note. Preserve its literal '
                  'name; use JSON null for unknown colour and absent note. Do not guess.')
    else:
        raise ValueError('Unknown live test case.')
    return {'model': model, 'max_tokens': 768, 'timeout': 60,
            'system': 'Answer only from the supplied information using the response schema.',
            'messages': [{'role': 'user', 'content': prompt}], 'response_schema': schema}


def check_output(case, text):
    """Check exact types and simple known answers without repairing model output.

    Args:
        case: Known test case name.
        text: Completed decoded response from the production caller.
    Returns:
        None on success; ValueError on schema, answer or evidence mismatch.
        Extraction applies the real validator, requires one fact with the exact
        supplied statement/quote, correct subject and city value, and resolves
        any object reference to Halifax. This narrow fixture is not a general
        entailment test. No model calls or writes occur.
    """
    if case == 'basic-types':
        result = BasicOutput.model_validate_json(text).model_dump()
        if result != {'count': 5, 'label': 'beads', 'present': True}:
            raise ValueError('Typed response did not match the known counting answer.')
    elif case == 'nested-null':
        result = NestedOutput.model_validate_json(text).model_dump()
        if result != {'items': [{'name': 'null', 'colour': None}], 'note': None}:
            raise ValueError('Nested output did not preserve literal text and unknown values.')
    elif case == 'source-extraction':
        result = validate_response(text, SOURCE)
        entities = {entity.id: entity.name for entity in result.entities}
        if len(result.facts) != 1:
            raise ValueError('Expected exactly one supported source fact.')
        fact = result.facts[0]
        if (fact.statement != SOURCE or fact.quote != SOURCE
                or entities[fact.subject_id] != 'Cedar Robotics' or fact.value != 'Halifax'
                or (fact.object_id is not None and entities[fact.object_id] != 'Halifax')):
            raise ValueError('Extracted fact did not match the supplied organization and city.')
    else:
        raise ValueError('Unknown live test case.')


def run_live(root, env_file, provider=None, model=None):
    """Execute each check once and persist private, independently inspectable results.

    Args:
        root: Output parent; a new private UUID run folder is created.
        env_file: Private dotenv configuration path.
        provider: Optional backend override; None uses normal configuration.
        model: Optional model override; None uses normal configuration.
    Returns:
        Pair of run directory and summary with one pass/fail entry per case.
        Makes at most three generation calls, no retries, and retains production
        request/response/usage receipts. Missing credentials or transport errors
        count as failures, never skips. Public summaries contain only error class
        and phase, not provider exception text or credentials. Configuration and
        filesystem errors propagate. No service is restarted or configuration edited.
    """
    sid = str(uuid4())
    folder = root / sid
    folder.mkdir(parents=True, mode=0o700)
    caller = ModelCalls(root, env_file, provider=provider, model=model)
    summary = {'provider': caller.provider, 'model': caller.model,
               'contract_id': caller.contract_id, 'started_at': datetime.now(timezone.utc).isoformat(),
               'suite_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'maximum_generation_calls': len(CASES), 'results': []}
    for case in CASES:
        phase = 'request'
        try:
            text = caller('live-' + case, build_request(case, caller.model), sid)
            phase = 'validation'
            check_output(case, text)
        except Exception as error:
            if phase == 'validation':
                caller.mark_validation('live-' + case, sid, False)
            result = {'case': case, 'passed': False, 'phase': phase,
                      'error_type': type(error).__name__}
        else:
            caller.mark_validation('live-' + case, sid, True)
            result = {'case': case, 'passed': True}
        summary['results'].append(result)
        print(json.dumps(result), flush=True)
    summary['passed'] = all(row['passed'] for row in summary['results'])
    save(folder / 'results.json', summary, '')
    return folder, summary


def main(argv=None):
    """Preview selected configuration or explicitly run the live smoke suite.

    Args:
        argv: Optional argument list; None reads command-line arguments. --live
            enables requests; --env-file, --provider, --model and --output-root
            follow the application configuration interface.
    Returns:
        Zero for preview/all passing; one for any failed live check. Configuration
        errors produce CLI errors. Preview reads selection but sends nothing and
        creates no artifacts. Prints only selection, safe statuses and receipt path.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--env-file', type=Path, default=Path.cwd() / '.env')
    parser.add_argument('--provider')
    parser.add_argument('--model')
    parser.add_argument('--output-root', type=Path, default=Path.cwd() / 'runs/live-model-tests')
    args = parser.parse_args(argv)
    config = resolve_config(args.env_file, args.provider, args.model)
    print(json.dumps({'live': args.live, 'provider': config.provider, 'model': config.model,
                      'cases': CASES, 'maximum_generation_calls': len(CASES)}), flush=True)
    if not args.live:
        return 0
    folder, summary = run_live(args.output_root, args.env_file, args.provider, args.model)
    print(f'Results: {folder}', flush=True)
    return int(not summary['passed'])


if __name__ == '__main__':
    raise SystemExit(main())
