"""Try to resolve the support problems found during claim triage.

Some claims might have missing or ambigous knowledge, which a 
triage step can flag. This section attempts to resolve the
missing details, either validating or rejecting the claim.

For each claim marked as needing review, this file uses claim_evidence_collection.py to
gather relevant passages from the saved documents. It then gives the AI the
original claim, the reason for review and those passages, asking whether the
specific problem has been addressed. For example, neighbouring text might supply
a location missing from the original quotation.

Accepted claims must cite supplied passages and are added to the information
available for the report. Rejections keep their reasons; technical failures remain
unreviewed rather than being called rejections. This stage saves the evidence and
decisions for its progress screen, without rewriting the original claim or
collecting new web sources. It checks source support, not independent truth.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from queue import Queue, Empty
from projectclarity.claim_review.concurrent_investigation import investigate_claim
from pydantic import BaseModel, ConfigDict
from projectclarity.claim_review.claim_evidence_collection import EvidenceCollector, checked_path
from projectclarity.knowledge_extraction.knowledge_extraction import save


class Citation(BaseModel):
    """Exact supporting text from one supplied saved passage."""
    model_config=ConfigDict(extra='forbid',strict=True)
    passage_id:str
    quote:str


class InvestigationDecision(BaseModel):
    """One assessment of an unchanged claim, not a replacement claim."""
    model_config=ConfigDict(extra='forbid',strict=True)
    fact_id:str
    decision:Literal['accept','reject']
    reason:str
    citations:list[Citation]


def validate_investigation(text, packet):
    """Check identity and literal citations before routing a model decision.

    Args: text: Raw response JSON; packet: Exact saved input evidence packet.
    Returns: Validated decision or ValueError. Does not prove semantic entailment.
    """
    decision=InvestigationDecision.model_validate_json(text)
    if decision.fact_id!=packet['claim']['fact_id'] or not decision.reason.strip():raise ValueError('Invalid claim ID or empty reason.')
    if decision.decision=='accept' and not decision.citations:raise ValueError('Acceptance requires citations.')
    passages={p['passage_id']:p for p in packet['passages']}
    for citation in decision.citations:
        if citation.passage_id not in passages or not citation.quote.strip() or citation.quote not in passages[citation.passage_id]['text']:
            raise ValueError('Citation is not an exact supplied source excerpt.')
    return decision


class InvestigationRun:
    """A real LangGraph investigation node with per-claim durable progress."""
    def __init__(self, root, state, publish, caller, stop):
        """Snapshot triage outcomes and initialize independent investigation artifacts.

        Args:
            root: Session directory containing immutable upstream artifacts.
            state: Research state with terminal triage and original extraction run.
            publish: Atomic session progress writer.
            caller: Existing model transport; injectable for offline tests.
            stop: Cooperative cancellation event.
        Returns: None; creates output snapshots, no model or network calls.
            Invalid triage partitions/context raise before any claims are routed.
            Explicit resume_investigation_from loads matching successful receipts;
            workers revalidate full requests/evidence and leave prior files intact.
        """
        self.state,self.publish,self.caller,self.stop=state,publish,caller,stop
        self.session_root=root
        triage_root=checked_path(root,state['triage']['artifact_directory'])
        raw=(triage_root/'decisions.json').read_bytes();data=json.loads(raw)
        if data['research_question']!=state['research_question']:raise ValueError('Triage question differs.')
        records=data['facts']
        if len({r['fact_id'] for r in records})!=len(records):raise ValueError('Duplicate triage records.')
        self.flagged=[r for r in records if r['triage_bucket']=='needs_verification']
        self.baseline=[r for r in records if r['triage_bucket']=='report_knowledge']
        if len(self.flagged)!=state['triage']['needs_verification'] or len(self.baseline)!=state['triage']['report_knowledge']:raise ValueError('Triage counts differ.')
        self.root=triage_root/('investigation-'+uuid4().hex[:12]);self.root.mkdir(mode=0o700)
        self.rows=[{'id':i+1,'record':r,'status':'pending'} for i,r in enumerate(self.flagged)]
        self.progress={'status':'running','artifact_directory':str(self.root.relative_to(root)),
            'started_at':datetime.now(timezone.utc).isoformat(),'claims_total':len(self.rows),
            'claims_finished':0,'accepted':0,'rejected':0,'failed':0,'model_calls':0,
            'concurrency':state.get('investigation_concurrency',6),'claims_reused':0,'active_claims':[],
            'report_knowledge':len(self.baseline),'triage_status':state['triage']['status'],
            'triage_unreviewed':len(data.get('unreviewed_fact_ids',[])),'current_claim':None,'events':[]}
        state['investigation']=self.progress
        self.prompt=(Path(__file__).resolve().parents[1]/'prompts/investigate_claims.txt').read_text()
        self.cached_claims = {}
        prior = state.pop('resume_investigation_from', None)
        if prior:
            previous = checked_path(root,prior)
            manifest = json.loads((previous/'manifest.json').read_text())
            if (manifest['triage_sha256'] != hashlib.sha256(raw).hexdigest()
                    or manifest['research_question'] != state['research_question']
                    or manifest['requested_information'] != state['information_needed']):
                raise ValueError('Investigation resume inputs changed.')
            for row in self.rows:
                path = previous/f'claim-{row["id"]:04d}.json'
                if path.exists():
                    receipt = json.loads(path.read_text())
                    if receipt.get('status') == 'checks_pass':
                        self.cached_claims[row['id']] = {**receipt,'_reused_from':str(path.relative_to(root.resolve()))}
        save(self.root/'manifest.json',{'research_question':state['research_question'],
            'requested_information':state['information_needed'],'triage_sha256':hashlib.sha256(raw).hexdigest(),
            'input':data,'prompt_sha256':hashlib.sha256(self.prompt.encode()).hexdigest(),
            'investigation_concurrency':self.progress['concurrency'],'resumed_from':prior,
            'passage_character_budget':30000,'model':caller.model,'scope':'Saved-source support; no external verification.'},'')
        self.write_store()

    def cancelled(self):
        """Stop scheduling at collection and request boundaries.

        Args: None.
        Returns: None or raises InterruptedError; in-flight results remain retained.
        """
        if self.stop.is_set():raise InterruptedError('Stopped by user.')

    def event(self, message):
        """Publish bounded live progress without exposing model credentials.

        Args: message: Human-readable transition.
        Returns: None; saves current session progress through the supplied writer.
        """
        self.progress['events']=(self.progress['events']+[{'time':datetime.now(timezone.utc).isoformat(),'message':message}])[-100:]
        self.publish(self.state)

    def write_store(self):
        """Save inspectable reviews and the combined report bucket incrementally.

        Args: None.
        Returns: None; atomically replaces each stage-owned output. Upstream stores
            are unchanged. Failed/pending records never enter report or rejected.
        """
        accepted=[{**r['record'],'investigation':r['assessment'],'supporting_evidence':r['supporting_evidence']} for r in self.rows if r['status']=='accepted']
        data={'research_question':self.state['research_question'],'status':self.progress['status'],
              'records':self.rows,'scope':'Source support, not independent confirmation of truth.'}
        outputs={'reviews.json':data,'report_knowledge.json':{'research_question':self.state['research_question'],'facts':self.baseline+accepted,'scope':data['scope']},
                 'rejected.json':{'facts':[r for r in self.rows if r['status']=='rejected']},
                 'needs_review.json':{'facts':[r for r in self.rows if r['status'] not in ('accepted','rejected')]}}
        for name,payload in outputs.items():
            temporary=self.root/(name+'.tmp');save(temporary,payload,'');temporary.replace(self.root/name)

    def investigate(self, graph_state):
        """Review independent claims in bounded slots with one shared-state writer.

        Args: graph_state: Owning session identity; corpus is snapshotted once.
        Returns: Unchanged identity. Coordinator alone updates ordered rows and
            report knowledge. Successful saved replies are revalidated against
            freshly collected identical evidence before reuse. On stop, workers
            drain before cancellation; failed/unstarted claims remain unreviewed.
        """
        if not self.rows:
            return graph_state
        limit = self.progress['concurrency']
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError('Investigation concurrency must be an integer from 1 to 16.')
        collector = EvidenceCollector(self.session_root,self.state,self.cancelled)
        inbox, active, next_row = Queue(), {}, 0

        def updates():
            """Apply worker notifications and expose all currently active claims.

            Args: None; drains this attempt's packet and call notifications.
            Returns: None; serializes progress, never initiates a model request.
            """
            changed = False
            while True:
                try:
                    kind, identity, packet = inbox.get_nowait()
                except Empty:
                    break
                changed = True
                row = self.rows[identity-1]
                if kind == 'packet':
                    row.update(packet=packet,status='reviewing')
                else:
                    self.progress['model_calls'] += 1
            active_claims = [{'id':row['id'],'statement':row['record']['fact']['statement'],
                'status':row['status']} for row in self.rows if row['status'] in ('collecting','reviewing')]
            if changed or active_claims != self.progress['active_claims']:
                self.progress['active_claims'] = active_claims
                self.progress['current_claim'] = None
                self.write_store(); self.publish(self.state)

        with ThreadPoolExecutor(max_workers=limit,thread_name_prefix='clarity-investigation') as pool:
            while active or (next_row < len(self.rows) and not self.stop.is_set()):
                while len(active) < limit and next_row < len(self.rows) and not self.stop.is_set():
                    row = self.rows[next_row]; next_row += 1; row['status'] = 'collecting'
                    future = pool.submit(investigate_claim,self,collector,dict(row),inbox,self.cached_claims.get(row['id']))
                    active[future] = row
                if not active:
                    break
                done, _ = wait(active,timeout=0.1,return_when=FIRST_COMPLETED)
                updates()
                for future in done:
                    row = active.pop(future); receipt = future.result(); updates()
                    if receipt.get('packet'):
                        row['packet'] = receipt['packet']
                    if receipt['status'] == 'checks_pass':
                        row.update(assessment=receipt['assessment'],supporting_evidence=receipt['supporting_evidence'],
                            status='accepted' if receipt['assessment']['decision']=='accept' else 'rejected')
                        self.progress[row['status']] += 1
                        self.progress['report_knowledge'] = len(self.baseline)+self.progress['accepted']
                        if receipt.get('reused_from'):
                            self.progress['claims_reused'] += 1
                    elif receipt['status'] == 'interrupted':
                        row['status'] = 'pending'
                    else:
                        row.update(status='failed',error='Review failed; this claim remains unreviewed. '+receipt['error']['type'])
                        self.progress['failed'] += 1
                    save(self.root/f'claim-{row["id"]:04d}.json',receipt,'')
                    if receipt['status'] != 'interrupted':
                        self.progress['claims_finished'] += 1
                    updates(); self.event(f'Claim {row["id"]}: {row["status"]}.')
        updates()
        self.cancelled()
        return graph_state

    def execute(self):
        """Execute the investigation stage once and retain honest terminal state.

        Args: None.
        Returns: None; publishes completed/partial/failed/cancelled and final stores.
            Empty input makes no model calls. Completion is not proof of truth. Holds
            active session status until automatic report handoff to prevent replay.
        """
        try:
            self.investigate({'session_id':self.state['session_id']})
            self.progress['status']='completed' if not self.progress['failed'] else ('partial' if self.progress['accepted']+self.progress['rejected'] else 'failed')
        except InterruptedError:self.progress['status']='cancelled'
        except Exception as error:self.progress.update(status='failed',error='Investigation could not continue: '+type(error).__name__)
        self.progress['active_claims']=[];self.progress['current_claim']=None;self.progress['finished_at']=datetime.now(timezone.utc).isoformat()
        self.state['status']='investigation_'+self.progress['status']
        if self.state.get('auto_report') and not self.stop.is_set() and self.progress['status'] in ('completed','partial'):
            self.state['status']='investigation_running'
        self.write_store()
        self.event(f'Investigation {self.progress["status"]}: {self.progress["accepted"]} accepted; {self.progress["rejected"]} rejected; {self.progress["failed"]} failed.')
        save(self.root/'summary.json',self.progress,'')
