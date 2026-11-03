"""Check whether selected claims have enough support to be considered for the report.

Each claim is sent to an LLM (along with its supporting evidence) and the LLM is 
asked if the claim is supported by the evidence, and if not, why. 

A fact can be relevant but still omit an important detail or cite text that does
not support its full meaning. After relevance selection, this file shows the AI
the selected claims and their original source text. It asks which claims can enter
the report's information collection and which need more investigation, with a
reason for each decision. Here, "triage" means deciding what needs further review.

Claims needing review go to claim_investigation.py. Failed requests remain
unreviewed rather than being accepted. This file also handles an explicit resume
of stopped triage, reusing matching saved replies where possible. It leaves the
original facts intact and does not search for new sources or prove independent truth.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from queue import Queue, Empty
from projectclarity.claim_review.concurrent_triage import review_batch

from pydantic import BaseModel, ConfigDict

from projectclarity.knowledge_extraction.knowledge_extraction import save

PROMPT = Path(__file__).resolve().parents[1] / 'prompts/triage_claims.txt'


class Decision(BaseModel):
    """A routing decision on an existing fact, not a replacement assertion."""
    model_config = ConfigDict(extra='forbid', strict=True)
    fact_id: str
    bucket: Literal['needs_verification', 'report_knowledge']
    reason: str


class Decisions(BaseModel):
    """Exactly one decision per supplied fact; coverage is checked separately."""
    model_config = ConfigDict(extra='forbid', strict=True)
    decisions: list[Decision]


def validate_decisions(text, records):
    """Reject unknown, duplicate or omitted fact IDs and empty routing explanations.

    Args:
        text: Unmodified model response JSON.
        records: Complete selected fact batch supplied to this request.
    Returns:
        Validated decision list. Raises ValueError on invalid/incomplete coverage;
        does not establish that the model's semantic judgment is correct.
    """
    result = Decisions.model_validate_json(text).decisions
    ids = [d.fact_id for d in result]
    expected = {r['fact_id'] for r in records}
    if len(ids) != len(set(ids)) or set(ids) != expected or any(not d.reason.strip() for d in result):
        raise ValueError('Triage must cover every supplied fact exactly once with a reason.')
    return result


class TriageRun:
    """One triage stage routing complete chunk-level selected batches."""

    def __init__(self, root, state, publish, caller, stop, resume_from=None):
        """Snapshot the selected store and group its records by extraction artifact.

        Args:
            root: Owning session directory.
            state: Saved research definition and completed/partial selection snapshot.
            publish: Atomic session snapshot writer.
            caller: Existing model transport exposing model and retaining receipts.
            stop: Cooperative cancellation event.
            resume_from: Optional prior stopped attempt directory, relative to root.
        Returns:
            None. Creates an isolated triage directory and empty output buckets.
            Invalid context or duplicate IDs raise before model calls. No record
            filtering, truncation, source downloads or outside searches occur.
        """
        self.state, self.publish, self.caller, self.stop = state, publish, caller, stop
        self.source_root = root / state['run'].get('artifact_directory', 'run')
        selection_root = root / state['selection']['artifact_directory']
        raw = (selection_root / 'relevant_knowledge.json').read_bytes()
        store = json.loads(raw)
        if store['research_question'] != state['research_question'] or store['requested_information'] != state['information_needed']:
            raise ValueError('Selected knowledge context differs from the saved definition.')
        self.records = store['facts']
        if len({r['fact_id'] for r in self.records}) != len(self.records):
            raise ValueError('Duplicate selected fact IDs.')
        if len(self.records) != state['selection']['facts_selected']:
            raise ValueError('Selected knowledge count differs from selection state.')
        groups = {}
        for record in self.records:
            key = (record['source']['artifact'], record['source']['sha256'])
            groups.setdefault(key, []).append(record)
        self.groups = list(groups.values())
        self.session_root=root
        self.recovery_root=None
        if resume_from:
            previous=(root/resume_from).resolve()
            if not previous.is_relative_to(root.resolve()):raise ValueError('Recovery path outside session')
            old=json.loads((previous/'manifest.json').read_text())
            if old['input_sha256']!=hashlib.sha256(raw).hexdigest() or old['research_question']!=state['research_question'] or old['requested_information']!=state['information_needed']:
                raise ValueError('Recovery inputs changed')
            if old['prompt_sha256']!=hashlib.sha256(PROMPT.read_bytes()).hexdigest() or old['model']!=caller.model:
                raise ValueError('Recovery model or prompt changed; cannot silently reuse decisions')
            self.recovery_root=previous
        self.root = selection_root / ('triage-' + uuid4().hex[:12]); self.root.mkdir(mode=0o700)
        self.decided = []
        self.triage = {'status':'running', 'artifact_directory':str(self.root.relative_to(root)),
            'started_at':datetime.now(timezone.utc).isoformat(), 'facts_total':len(self.records),
            'facts_reviewed':0, 'needs_verification':0, 'report_knowledge':0, 'model_calls':0, 'reused_batches':0,
            'concurrency':state.get('triage_concurrency',6), 'batches_finished':0, 'batches_passed':0, 'batches_total':len(self.groups), 'events':[],
            'upstream_status':state['run']['status'], 'selection_status':state['selection']['status'],
            'batches':[{'id':i+1, 'source':group[0]['source'], 'facts':len(group), 'status':'pending'} for i,group in enumerate(self.groups)]}
        state['triage'] = self.triage
        self.prompt = PROMPT.read_text()
        save(self.root / 'manifest.json', {'research_question':state['research_question'],
            'requested_information':state['information_needed'], 'input':store,
            'input_sha256':hashlib.sha256(raw).hexdigest(), 'prompt_sha256':hashlib.sha256(self.prompt.encode()).hexdigest(),
            'model':caller.model, 'max_output_tokens':8192, 'triage_concurrency':self.triage['concurrency'], 'resumed_from':resume_from, 'scope':'Original-source support and completeness only.'}, '')
        self.write_buckets()

    def recovered_response(self, batch_id, request):
        """Find an already saved answer so resuming does not needlessly pay for it again.
        
        A committed batch is preferred. If the process stopped before committing the
        batch, a complete provider receipt may still supply the answer. Only the
        exact same request may be reused; review applies the normal validator again.
        
        Args:
            batch_id: One-based source-group number, stable for this unchanged input.
            request: Full current provider payload to compare with the saved request.
        Returns:
            A pair of raw model text and its receipt path relative to the session,
            or None when recovery is disabled or no completed answer was saved.
            Raises for a changed request, a committed failed batch or multiple complete
            candidate receipts. Reads old files without modifying them or calling the
            model. None does not establish that the provider never processed a lost
            request; it only means there is no saved answer available here.
        """
        if self.recovery_root is None:return None
        path=self.recovery_root/f'batch-{batch_id:04d}.json'
        if path.exists():
            receipt=json.loads(path.read_text())
            if receipt.get('request')!=request:raise ValueError('Saved batch request differs from recovery input')
            if receipt['status']!='checks_pass':raise ValueError('Previously failed batch retained as unreviewed; resume does not retry completed failures')
            return receipt['raw_text'],str(path.relative_to(self.session_root))
        matches=[]
        for path in self.session_root.glob(f'triage-{self.recovery_root.name}-batch{batch_id}-*.json'):
            receipt=json.loads(path.read_text())
            if receipt.get('request')!=request:raise ValueError('Provider recovery request mismatch')
            if receipt.get('response',{}).get('stop_reason')=='end_turn' and receipt.get('raw_text'):
                matches.append((receipt['raw_text'],str(path.relative_to(self.session_root))))
        if len(matches)>1:raise ValueError('Multiple completed responses require explicit reconciliation')
        return matches[0] if matches else None

    def event(self, message):
        """Publish observed stage progress and retain a small activity history.

        Args: message: Credential-free progress description.
        Returns: None; updates the saved session without starting other work.
        """
        self.triage['events'] = (self.triage['events'] + [{'time':datetime.now(timezone.utc).isoformat(), 'message':message}])[-100:]
        self.publish(self.state)

    def write_buckets(self):
        """Atomically save decisions and the two original-record output buckets.

        Args: None.
        Returns:
            None. Saves copies, never modifies original knowledge. Unreviewed facts
            remain separate from both buckets. Each file is atomically replaced;
            public counts are published only after these writes finish.
        """
        decided_ids = {r['fact_id'] for r in self.decided}
        data = {'research_question':self.state['research_question'], 'status':self.triage['status'],
            'facts':self.decided, 'unreviewed_fact_ids':[r['fact_id'] for r in self.records if r['fact_id'] not in decided_ids],
            'scope':'Source-support triage; no independent verification or repair.'}
        outputs = {'decisions.json':data}
        for bucket in ('needs_verification','report_knowledge'):
            outputs[bucket+'.json'] = {**data, 'facts':[r for r in self.decided if r['triage_bucket']==bucket]}
        for name, payload in outputs.items():
            temporary = self.root / (name+'.tmp'); save(temporary,payload,''); temporary.replace(self.root/name)

    def review(self, graph_state):
        """Review independent source groups in bounded slots with serialized saves.

        Args: graph_state: Owning session identity supplied by the research graph.
        Returns: Unchanged graph state. Only this coordinator updates decisions,
            bucket totals and files, retaining original group/record order. Failed
            groups remain unreviewed. Cancellation drains in-flight results before
            raising; no further groups are scheduled. Recovery policy is unchanged.
        """
        limit = self.triage['concurrency']
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError('Triage concurrency must be an integer from 1 to 16.')
        jobs = list(zip(self.triage['batches'], self.groups))
        inbox, active, results, next_job = Queue(), {}, {}, 0

        def updates():
            """Publish actual dispatch counts on the sole session-writing thread.

            Args: None; drains this attempt's private notification queue.
            Returns: None; no request or retry is initiated by this callback.
            """
            changed = False
            while True:
                try:
                    inbox.get_nowait()
                except Empty:
                    break
                self.triage['model_calls'] += 1; changed = True
            if changed:
                self.publish(self.state)

        with ThreadPoolExecutor(max_workers=limit, thread_name_prefix='clarity-triage') as pool:
            while active or (next_job < len(jobs) and not self.stop.is_set()):
                while len(active) < limit and next_job < len(jobs) and not self.stop.is_set():
                    batch, records = jobs[next_job]; next_job += 1
                    batch['status'] = 'reviewing'
                    future = pool.submit(review_batch, self, dict(batch), records, inbox)
                    active[future] = (batch, records)
                    self.event(f'Checking source {batch["source"]["document_id"]}, chunk {batch["source"]["chunk_index"]+1}: {len(records)} selected facts.')
                if not active:
                    break
                done, _ = wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
                updates()
                for future in done:
                    batch, records = active.pop(future)
                    receipt = future.result(); updates()
                    if receipt is None:
                        batch['status'] = 'cancelled'
                        continue
                    if receipt['status'] == 'checks_pass':
                        decisions = {d['fact_id']:d for d in receipt['decisions']}
                        results[batch['id']] = [{**record,
                            'triage_bucket':decisions[record['fact_id']]['bucket'],
                            'triage_reason':decisions[record['fact_id']]['reason']} for record in records]
                        self.decided = [record for key in sorted(results) for record in results[key]]
                        for record in results[batch['id']]:
                            self.triage[record['triage_bucket']] += 1
                        if receipt.get('recovered_from'):
                            self.triage['reused_batches'] += 1
                        batch['status'] = 'completed'
                        self.triage['facts_reviewed'] += len(records)
                        self.triage['batches_passed'] += 1
                    else:
                        batch.update(status='failed',error='This batch could not be checked. Its facts remain unreviewed.')
                    save(self.root/f'batch-{batch["id"]:04d}.json',receipt,'')
                    self.triage['batches_finished'] += 1; self.write_buckets()
                    self.event(f'Batch {batch["id"]}/{self.triage["batches_total"]}: {batch["status"]}.')
        updates()
        if self.stop.is_set():
            raise InterruptedError('Stopped by user; in-flight triage results saved.')
        return graph_state

    def execute(self):
        """Run triage once and publish honest completion independently of upstream work.

        Args: None.
        Returns:
            None. Persists completed/partial/failed/cancelled status and both buckets.
            Empty input succeeds without calls. Does not repair claims, retrieve
            more passages, independently corroborate facts or generate a report. Holds
            the active session status during an authorized automatic investigation
            handoff to prevent duplicate work from being queued.
        """
        try:
            self.event(f'Checking completeness and evidence for {len(self.records)} selected facts.')
            self.review({'session_id':self.state['session_id']})
            passed,total=self.triage['batches_passed'],self.triage['batches_total']
            self.triage['status']='completed' if passed==total else ('partial' if passed else 'failed')
        except InterruptedError: self.triage['status']='cancelled'
        except Exception: self.triage.update(status='failed',error='Triage stopped unexpectedly; completed decisions are retained.')
        self.triage['finished_at']=datetime.now(timezone.utc).isoformat()
        self.state['status']='triage_'+self.triage['status']
        if self.state.get('auto_investigate') and not self.stop.is_set() and self.triage['status'] in ('completed','partial'):
            self.state['status']='triage_running'
        self.write_buckets()
        self.event(f'Triage {self.triage["status"]}: {self.triage["needs_verification"]} need verification; {self.triage["report_knowledge"]} in Report Knowledge.')
        save(self.root/'summary.json',self.triage,'')
