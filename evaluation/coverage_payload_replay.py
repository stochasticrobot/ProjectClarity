"""Replay a private saved coverage-overflow fixture without any live model request.

Run `python -m evaluation.coverage_payload_replay --fixture PATH --output PATH`.
The fixture holds an immutable original run, hashes, a reconstructed failure-stage
session and the baseline direct/aggregate inputs captured before normalization.
It is deliberately not distributed with the public tests: those files contain
owner research data. A missing or changed fixture fails, never silently skips.

The production assessment node, serializer, SDK parser, evidence validation and
persistence run on a temporary copy. Only HTTP replies are synthetic. The reply
deliberately says unknown, so dispatch success cannot become invented research
completion. The original source/run files and frozen fixture stay untouched.
This proves bounded production transport and complete manifest accounting, not
semantic preservation by model summaries. Historical normalization is checked separately.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import socket
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx2

from projectclarity.coverage import contract_assessment, bounded_assessment
from projectclarity.coverage.payload_normalization import (
    ENCODING, expand_qualification_payload, prepare_coverage_messages)
from projectclarity.coverage.stopping import completion_status
from projectclarity.research_schemas import ResearchSession


def file_hashes(directory):
    """Fingerprint every fixture file to detect any replay mutation.

    Args:
        directory: Existing private fixture directory, never a URL.
    Returns:
        Relative-path SHA256 mapping of its files. No files are changed.
    """
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file()}


def replay(fixture):
    """Verify frozen failure inputs and dispatch bounded coverage through production.

    Args:
        fixture: Directory containing original-manifest.json, original/RUN_ID,
            reconstructed-session.json and baseline-payload-{0,1}.json.
    Returns:
        Result measurements after all assertions pass. Raises for missing/modified
        receipts, missing accounting, oversize input, unexpected calls or false completion.
        Temporary replay state has a fresh time allowance and fake credential;
        original settings/receipts are unchanged. No real provider is contacted.
    """
    before = file_hashes(fixture)
    input_hashes = json.loads((fixture / 'fixture-input-hashes.json').read_text())
    if any(before.get(name) != digest for name, digest in input_hashes.items()):
        raise AssertionError('Frozen replay inputs changed')
    manifest = json.loads((fixture / 'original-manifest.json').read_text())
    original = fixture / 'original' / manifest['run_id']
    if file_hashes(original) != manifest['files']:
        raise AssertionError('Original fixture receipt hashes changed')
    baseline = [json.loads((fixture / f'baseline-payload-{i}.json').read_text()) for i in range(2)]
    sizes = []
    for payload in baseline:
        messages, receipt = prepare_coverage_messages(contract_assessment.SYSTEM_PROMPT,
            payload, contract_assessment.MAX_COVERAGE_CHARACTERS)
        assert receipt['original_characters'] > contract_assessment.MAX_COVERAGE_CHARACTERS
        assert receipt['effective_characters'] <= contract_assessment.MAX_COVERAGE_CHARACTERS
        assert expand_qualification_payload(json.loads(messages[1][1])) == payload
        sizes.append(receipt)
    captured = []
    session = ResearchSession.model_validate_json((fixture / 'reconstructed-session.json').read_text())
    # This is an isolated test, not a resume that replenishes the real run.
    session.retrieval_usage = {}
    session.status = 'sources_retrieved'
    initial_usage = session.usage.requests_attempted

    def respond(request):
        """Return deliberately inconclusive judgments at the real SDK HTTP boundary.

        Args:
            request: Serialized httpx2 request from the production model adapter.
        Returns:
            Provider-shaped response with unknown coverage. Unexpected task schemas
            fail immediately; no lookup, live model or source fetch occurs.
        """
        wire = json.loads(request.content)
        title = wire['output_config']['format']['schema']['title']
        assert title in {'CoverageAssessment', 'BoundedDigest'}
        encoded = json.loads(wire['messages'][0]['content'])
        decoded = expand_qualification_payload(encoded)
        measured = len(json.dumps([('system', contract_assessment.SYSTEM_PROMPT if title == 'CoverageAssessment' else bounded_assessment.SYSTEM_PROMPT), ('human', wire['messages'][0]['content'])], ensure_ascii=False)) + len(json.dumps(wire['output_config']['format']['schema'], ensure_ascii=False))
        captured.append(dict(title=title, wire=encoded, expanded=decoded, characters=measured))
        if title == 'BoundedDigest':
            pool = [c['candidate_id'] for c in decoded['inputs']] if decoded['stage']=='evidence' else [cid for c in decoded['inputs'] for cid in c['witness_candidate_ids']]
            answer = dict(covered_input_ids=[c['input_id'] for c in decoded['inputs']], summary='Offline transport probe; no semantic assessment.', witness_candidate_ids=list(dict.fromkeys(pool))[:24], limitations=['Semantic sufficiency untested.'], unresolved_conflict=False, unresolved_identity=False, requires_detail=True)
        else:
            answer = dict(assessments=[dict(criterion_id=c['criterion_id'], judgment='unknown',
            items=[], target_count=c['target_count'], result_goal_id=c.get('result_goal_id'),
            benchmark_candidate_ids=[], evidence_candidate_ids=[],
            gaps=['Offline transport probe: semantic sufficiency has not been evaluated.'],
            rationale='Synthetic unknown judgment; no live model assessment.')
            for c in decoded['criteria']], next_searches=[], scope_estimates=[])
        return httpx2.Response(200, json=dict(id='offline-coverage', type='message', role='assistant',
            model=session.request.model, content=[dict(type='text', text=json.dumps(answer))],
            stop_reason='end_turn', stop_sequence=None, usage=dict(input_tokens=1, output_tokens=1)))

    with TemporaryDirectory(prefix='clarity-coverage-replay-') as temporary:
        root = Path(temporary)
        shutil.copytree(original, root / session.session_id)
        with httpx2.Client(transport=httpx2.MockTransport(respond)) as client, \
                patch('langchain_anthropic.chat_models._get_default_httpx_client', return_value=client), \
                patch.object(socket.socket, 'connect', side_effect=AssertionError('Replay forbids network')):
            for _ in range(200):
                contract_assessment.assess_contract({'session': session}, root,
                    key_reader=lambda: 'offline-coverage-test-key')
                if session.coverage['assessment_status'] != 'reviewing':
                    break
        entry = session.coverage['history'][-1]
        assert entry['status'] == 'assessed' and entry['error_code'] is None, entry
        assert entry['input_mode'] == 'bounded_tree'
        assert session.usage.requests_attempted == initial_usage + len(captured)
        assert not completion_status(session, root)['ready']
        final = captured[-1]['expanded']
        assert final['candidates'] == []
        assert len(final['evidence_batches']) == 1
        assert len(final['evidence_registry']) <= 24
        assert all('items' not in ledger for ledger in final['result_inventory'].values())
        assert entry['bounded_manifest']['candidate_count'] == len(entry['accounted_candidate_ids'])
        preflights = [json.loads(path.read_text()) for path in (root/session.session_id/'input_receipts').glob('*.json')]
        assert preflights and all(not row['errors'] and row['total_characters'] <= row['limit_characters'] for row in preflights)
        saved = ResearchSession.model_validate_json((root / session.session_id / 'session.json').read_text())
        assert saved.coverage['history'][-1]['bounded_manifest'] == entry['bounded_manifest']
        result = dict(status='passed', historical_normalization_sizes=sizes,
            production_manifest=entry['bounded_manifest'],
            max_summary_characters=max(row['total_characters'] for row in preflights if row['task']=='bounded_evidence'),
            final_characters=entry['input_contract']['total_characters'],
            candidate_total=entry['candidate_total'], accounted_candidates=len(entry['accounted_candidate_ids']),
            offered_witnesses=len(entry['offered_candidate_ids']),
            synthetic_model_calls=len(captured), live_model_calls=0,
            completed_research=False, original_receipt_files=len(manifest['files']),
            fixture_provenance=json.loads((fixture / 'provenance.json').read_text()))
    assert file_hashes(fixture) == before, 'Replay mutated its frozen fixture'
    return result


def main(argv=None):
    """Run the explicit private-fixture gate and write a separate inspectable result.

    Args:
        argv: Optional CLI argument sequence; None uses the process arguments.
    Returns:
        Zero after success. Missing data/assertions raise rather than produce a pass
        or skip. Writes only the requested output, outside the immutable fixture.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.resolve().is_relative_to(args.fixture.resolve()):
        raise ValueError('Replay output must be outside the immutable fixture')
    result = replay(args.fixture)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
