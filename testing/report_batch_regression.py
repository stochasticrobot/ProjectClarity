"""Replay saved report evidence offline through the actual production graph.

This command copies only the investigation inputs into a temporary session, uses
explicitly simulated model replies/counts, and never contacts a provider or changes
the original session. It checks routing, payload bounds and exact fact accounting,
not whether a real model would write a good report. Missing input is an error.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
from projectclarity.reporting.report_payloads import compact_packet, request_size, canonical_json
from projectclarity.reporting.report_generation import ReportRun
from projectclarity.workflow.research_sessions import Sessions
from tests.integration.test_batched_reporting import BatchCaller


def replay(source, output):
    """Run bounded report generation on a private disposable copy of saved evidence.

    Args:
        source: Existing session directory containing session.json and investigation.
        output: New audit JSON path outside the source session.
    Returns: Metrics written to output and returned. Verifies source hashes before
        and after, rejects an existing output, and disables outbound socket connects.
        Model replies/counts are fixtures; token savings and semantic quality are
        not measured. The report graph is real, but upstream stages are not replayed.
    """
    original = json.loads((source / 'session.json').read_text())
    inputs = source / original['investigation']['artifact_directory']
    paths = [source/'session.json', inputs/'manifest.json', inputs/'report_knowledge.json']
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    with TemporaryDirectory() as tmp, patch('socket.socket.connect', side_effect=AssertionError('Network disabled in offline report replay')):
        root = Path(tmp)
        state = copy.deepcopy(original)
        sid = state['session_id']
        destination = root / sid / state['investigation']['artifact_directory']
        destination.mkdir(parents=True)
        for name in ('manifest.json','report_knowledge.json'):
            shutil.copyfile(inputs/name, destination/name)
        # No prior report is reused: this regression must exercise every new task.
        state.pop('report', None)
        state.pop('report_history', None)
        state['status'] = 'report_running'
        caller = BatchCaller()
        sessions = Sessions(root, caller)
        try:
            sessions.write(state)
            sessions.execute_research(state, Event(), 'report')
            result = sessions.read(sid)
            if result['status'] != 'report_completed':
                raise AssertionError(result['report'])
            report_root = root/sid/result['report']['artifact_directory']
            manifest = json.loads((report_root/'manifest.json').read_text())
            report = sessions.report_data(sid)
            facts = manifest['packet']['facts']
            map_requests = [request for stage,request,_ in caller.calls if '-facts-' in stage]
            offered = [fact['id'] for request in map_requests for fact in json.loads(request['messages'][0]['content'])['facts']]
            expected = {fact['id'] for fact in facts}
            assert len(offered) == len(set(offered)) == len(expected)
            assert set(offered) == expected
            for fact in facts:
                projected = compact_packet(manifest['packet'], [fact])
                packed = projected['facts'][0]
                assert packed['fact'] == fact['fact']
                assert [projected['evidence_passages'][ref] for ref in packed['evidence']] == fact['evidence']
            measurements = {'scope':'Offline graph proof with simulated model responses and counts; not live semantic validation.',
                'source_session':sid, 'facts':len(facts), 'facts_accounted':result['report']['facts_accounted'],
                'generation_calls':len(caller.calls), 'map_calls':len(map_requests),
                'merge_calls':len(caller.calls)-len(map_requests),
                'input_limit':result['report']['input_token_limit'],
                'largest_offline_count':result['report']['largest_counted_input'],
                'largest_request_utf8_bytes':max(request_size(request) for _,request,_ in caller.calls),
                'original_packet_utf8_bytes':len(canonical_json(manifest['packet']).encode()),
                'all_compact_facts_utf8_bytes':len(canonical_json(compact_packet(manifest['packet'],facts)).encode()),
                'entries':len(report['entries']), 'original_hashes':hashes}
        finally:
            sessions.close()
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==value for path,value in hashes.items())
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x') as stream:
        json.dump(measurements,stream,indent=2)
    output.chmod(0o600)
    return measurements


def main():
    """Parse explicit source/output paths and run the offline-only regression.

    Args: None; paths come from command-line options.
    Returns: None; prints bounded metrics. Invalid paths and failures exit nonzero.
    """
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(replay(args.session.resolve(),args.output.resolve()),indent=2))


if __name__=='__main__':
    main()
