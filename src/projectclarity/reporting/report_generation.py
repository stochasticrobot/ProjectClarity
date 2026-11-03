"""Turn the collected, reviewed information into an answer the user can inspect.

Earlier steps gather and check individual claims. This final research step gives
the AI the report's saved facts and the answer requirements defined at the start,
asking it to filter bounded batches into cited answer items. Code combines those
items and presents the requested fields without narrative or model merge calls.
Legacy report artifacts remain readable. For a list of companies, those original
requirements determine what information each company entry should contain.

The code checks that entries address the required fields, refer to supplied facts
and account for input facts that were left out. It saves the readable report data
and a CSV file for spreadsheets, including incomplete candidates and stated gaps.
The web report page displays these results. This file does not collect missing
evidence, and its checks cannot guarantee that the AI interpreted every fact correctly.
"""
import csv
from datetime import datetime, timezone
import hashlib
from io import StringIO
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict
from projectclarity.claim_review.claim_evidence_collection import checked_path
from projectclarity.knowledge_extraction.knowledge_extraction import save


class CitedText(BaseModel):
    """A narrative assertion tied to supplied report fact IDs."""
    model_config=ConfigDict(extra='forbid',strict=True)
    text:str
    fact_ids:list[str]


class ReportField(BaseModel):
    """An answer to one unchanged Step1 requirement, including explicit uncertainty."""
    model_config=ConfigDict(extra='forbid',strict=True)
    requirement_id:str
    status:Literal['supported','unknown','insufficient','conflicting']
    value:str
    fact_ids:list[str]


class ReportEntry(BaseModel):
    """One answer entry with every requested field and a sourced identifying label."""
    model_config=ConfigDict(extra='forbid',strict=True)
    label:str
    label_fact_ids:list[str]
    fields:list[ReportField]


class Exclusion(BaseModel):
    """An input fact deliberately not used, with the reason retained for inspection."""
    model_config=ConfigDict(extra='forbid',strict=True)
    fact_id:str
    reason:str


class ReportResult(BaseModel):
    """One report with cited narrative and explicit used/excluded input accounting."""
    model_config=ConfigDict(extra='forbid',strict=True)
    summary:list[CitedText]
    entries:list[ReportEntry]
    gaps:list[str]
    exclusions:list[Exclusion]


def validate_report(text, packet):
    """Enforce exact requirements, citations and full input accounting before display.

    Args: text: Raw model JSON; packet: Complete requirements/evidence input.
    Returns: Structured report or ValueError. Checks provenance and completeness,
        not whether a model's synthesis is semantically correct. Unknown fields
        cannot contain invented values; incomplete entries cannot become matches.
        Validly cited facts count as used: redundant exclusion entries are removed
        from the returned structure only. Supplied IDs tolerate unambiguous case
        and surrounding whitespace differences. The raw response is unchanged.
    """
    from projectclarity.reporting.report_reference_normalization import normalize_references
    parsed=ReportResult.model_validate_json(text).model_dump()
    result=ReportResult.model_validate(normalize_references(parsed,
        [f["id"] for f in packet["facts"]], [r["id"] for r in packet["requirements"]]))
    allowed={f['id'] for f in packet['facts']};required={r['id'] for r in packet['requirements']};used=set()
    def references(ids, necessary=False):
        """Validate citation IDs and collect the set used by report assertions.

        Args: ids: Supplied fact references; necessary: Require at least one.
        Returns: None; raises for missing/unknown/duplicate IDs, without mutation
            beyond adding valid references to this validation's used set.
        """
        if (necessary and not ids) or len(ids)!=len(set(ids)) or not set(ids)<=allowed:raise ValueError('Missing or invalid report citations.')
        used.update(ids)
    for part in result.summary:
        if not part.text.strip():raise ValueError('Empty summary assertion.')
        references(part.fact_ids,True)
    for entry in result.entries:
        if not entry.label.strip():raise ValueError('Empty report label.')
        references(entry.label_fact_ids,True)
        ids=[f.requirement_id for f in entry.fields]
        if len(ids)!=len(set(ids)) or set(ids)!=required:raise ValueError('Report must preserve every requested field exactly once.')
        for field in entry.fields:
            references(field.fact_ids,field.status in ('supported','conflicting') or bool(field.value.strip()))
            if field.status=='supported' and not field.value.strip():raise ValueError('Supported field has no value.')
            if field.status=='unknown' and field.value.strip():raise ValueError('Unknown field must not invent a value.')
    excluded=[r.fact_id for r in result.exclusions]
    if len(excluded)!=len(set(excluded)) or any(not r.reason.strip() for r in result.exclusions):raise ValueError('Duplicate or unexplained exclusion.')
    # Citation checks above must pass before a used fact can override exclusion.
    result.exclusions = [row for row in result.exclusions if row.fact_id not in used]
    if used|set(excluded)!=allowed:raise ValueError('Every fact must be used or explicitly excluded.')
    return result.model_dump()


def csv_value(value):
    """Protect spreadsheet exports from treating untrusted cell text as formulas.

    Args: value: Generated/source display text.
    Returns: Text, prefixed with an apostrophe when formula-like after whitespace.
    """
    text=str(value)
    return "'"+text if text.lstrip().startswith(('=','+','-','@')) or text.startswith(('\t','\r','\n')) else text


def report_csv(report):
    """Render all entries with the same requirement columns as the readable report.

    Args: report: Validated report artifact with requirements and entries.
    Returns: UTF8-ready CSV text; includes completeness, statuses and citations.
        Incomplete entries are explicitly labeled, never silently dropped.
    """
    stream=StringIO();writer=csv.writer(stream)
    writer.writerow(['Entry','Completeness',*[csv_value(r['name']) for r in report['requirements']],*[csv_value(r['name']+' — status / citations') for r in report['requirements']], 'Source URLs'])
    fact_map={f['id']:f for f in report.get('facts',[])}
    for entry in report['entries']:
        cited=set(entry.get('label_fact_ids',[]))|{i for f in entry['fields'] for i in f['fact_ids']}
        urls=sorted({e['source_url'] for i in cited for e in fact_map.get(i,{}).get('evidence',[])})
        fields={f['requirement_id']:f for f in entry['fields']};complete=all(f['status']=='supported' for f in entry['fields'])
        writer.writerow([csv_value(entry['label']),'Supported match' if complete else 'Incomplete candidate',
            *[csv_value(fields[r['id']]['value'] or fields[r['id']]['status']) for r in report['requirements']],
            *[csv_value(fields[r['id']]['status']+' / '+', '.join(fields[r['id']]['fact_ids'])) for r in report['requirements']], '; '.join(urls)])
    return stream.getvalue()


class ReportRun:
    """One report stage preparing a report from the full saved report bucket."""
    def __init__(self, root, state, publish, caller, stop):
        """Snapshot reviewed facts across rounds and exact final requirements without filtering.

        Args: root: Session directory; state: Saved final definition/investigation;
            publish: Atomic state writer; caller: Shared audited model transport;
            stop: Cooperative cancellation event.
        Returns: None; writes immutable manifest. Invalid context, duplicate fact
            IDs or missing requirements fail initialization. No external work yet.
        """
        self.state,self.publish,self.caller,self.stop=state,publish,caller,stop
        self.item_report = state.get('report_scheme') != 'legacy'
        self.session_root = root
        from projectclarity.workflow.research_rounds import report_evidence
        folder, expected_facts, round_coverage = report_evidence(root, state)
        manifest=json.loads((folder/'manifest.json').read_text())
        if manifest['research_question']!=state['research_question'] or manifest['requested_information']!=state['information_needed']:
            raise ValueError('Final requirements differ from the investigated research definition.')
        raw=(folder/'report_knowledge.json').read_bytes();store=json.loads(raw)
        if store['research_question']!=state['research_question']:raise ValueError('Report knowledge question differs.')
        self.evidence_hash = hashlib.sha256(raw).hexdigest()
        records=store['facts']
        if len(records)!=expected_facts:raise ValueError('Report Knowledge count differs from saved investigation.')
        if len({r['fact_id'] for r in records})!=len(records):raise ValueError('Duplicate report facts.')
        if not state['information_needed']:raise ValueError('No answer requirements available.')
        self.root=folder/('report-'+uuid4().hex[:12]);self.root.mkdir(mode=0o700)
        self.requirements=[{'id':f'R{i+1}',**r} for i,r in enumerate(state['information_needed'])]
        facts=[]
        for i,record in enumerate(records):
            evidence=[{'quote':record['fact']['quote'],'source_url':record['source']['source_url'],'pages':record['source'].get('pages',[])}]
            for p in record.get('supporting_evidence',[]):
                item={'quote':p['quote'],'source_url':p['source_url'],'pages':p.get('pages',[])}
                if item not in evidence:evidence.append(item)
            facts.append({'id':f'F{i+1}','original_fact_id':record['fact_id'],'fact':record['fact'],
                          'entities':record.get('entities',[]),'evidence':evidence})
        self.packet={'research_question':state['research_question'],'requirements':self.requirements,'facts':facts,
            'coverage':{k:state.get(k,{}).get('status','not_started') for k in ('run','selection','triage','investigation')}}
        self.packet['coverage']['unreviewed_investigation']=state['investigation'].get('claims_total',0)-state['investigation'].get('accepted',0)-state['investigation'].get('rejected',0)
        self.packet['coverage']['unreviewed_triage']=state['investigation'].get('triage_unreviewed',0)
        if round_coverage:
            self.packet['coverage']['rounds'] = round_coverage
            for key in ('unreviewed_investigation', 'unreviewed_triage'):
                self.packet['coverage'][key] = sum(item[key] for item in round_coverage)
        self.progress={'status':'running','artifact_directory':str(self.root.relative_to(root)),
            'concurrency':state.get('report_selection_concurrency',6),
            'facts_total':len(facts),'facts_accounted':0,'entries':0,'complete_entries':0,'incomplete_entries':0,
            'model_calls':0,'started_at':datetime.now(timezone.utc).isoformat()}
        state['report']=self.progress
        prompt_root = Path(__file__).resolve().parents[1] / 'prompts'
        self.prompt_hashes = {name: hashlib.sha256((prompt_root / name).read_bytes()).hexdigest()
                              for name in (('prepare_report.txt','prepare_report_batch.txt') if self.item_report else ('legacy_prepare_report_batch.txt', 'merge_report_entries.txt', 'merge_report_summary.txt'))}
        save(self.root/'manifest.json',{'input_sha256':hashlib.sha256(raw).hexdigest(),'original_records':records,
             'packet':self.packet,'prompt_sha256':self.prompt_hashes,
             'report_selection_concurrency':self.progress['concurrency'],
             'policy':'V3: bounded evidence filters; code-only item assembly; no narrative or model merges.' if self.item_report else 'V2: legacy bounded narrative reporting.'},'')

    def prepare(self, graph_state):
        """Generate cited answer items through bounded filters and code-only assembly.

        Explicit legacy fixtures retain the previous narrative/merge path.
        
        The final requirements define the answer fields. Each input fact must be
        used or explicitly excluded so an apparently tidy report cannot silently
        hide dropped evidence. These checks enforce coverage and references, not
        semantic accuracy or independent truth.
        
        Args:
            graph_state: LangGraph state carrying the session identity; report facts,
                requirements and the prompt were captured when this instance was made.
        Returns:
            The unchanged graph_state. Saves report.json and report.csv after the
            generated result passes validation. Empty input creates explicit empty
            findings without a model call. generation.json records success or failure,
            with a safe failure reason. Individual task receipts retain raw replies,
            token counts, request hashes and reuse provenance even on rejection.
            Cancellation before dispatch raises InterruptedError. Provider, validation
            and file errors propagate to execute. Only an explicit retry can reuse
            earlier validated tasks and resend failed work; input is never truncated.
        """
        if self.stop.is_set():raise InterruptedError('Stopped before report request.')
        receipt={'status':'failed','attempted_calls':0}
        try:
            if self.packet['facts']:
                from projectclarity.reporting.batched_reporting import BoundedReportBuilder
                from projectclarity.reporting.item_reporting import ItemReportBuilder
                builder = ItemReportBuilder if self.item_report else BoundedReportBuilder
                result = builder(self, self.evidence_hash).build()
                receipt['attempted_calls'] = self.progress['model_calls']
            else:
                result={'summary':[],'entries':[],'gaps':['No source-supported knowledge is available to answer the requested fields.'],'exclusions':[]}
            report={**result,'presentation':'items' if self.item_report else 'legacy','research_question':self.state['research_question'],'requirements':self.requirements,
                    'facts':self.packet['facts'],'coverage':self.packet['coverage'],
                    'scope':'Cited synthesis of collected sources; not independently verified truth.'}
            save(self.root/'report.json',report,'')
            path=self.root/'report.csv';path.write_text(report_csv(report),encoding='utf-8-sig');path.chmod(0o600)
            complete=sum(all(f['status']=='supported' for f in e['fields']) for e in result['entries'])
            self.progress.update(facts_accounted=len(self.packet['facts']),entries=len(result['entries']),complete_entries=complete,incomplete_entries=len(result['entries'])-complete)
            receipt['status']='checks_pass'
        except Exception as error:
            receipt['error']={'type':type(error).__name__,'message':str(error)[:500]};raise
        finally:
            receipt['attempted_calls'] = self.progress['model_calls']
            save(self.root/'generation.json',receipt,'')
        return graph_state

    def execute(self):
        """Run the report stage once, publishing only validated results.

        Args: None.
        Returns: None; completed/failed/cancelled state. No automatic retries or dropped input.
            A finished in-flight result is retained if stop was requested mid-call.
        """
        try:
            self.prepare({'session_id':self.state['session_id']});self.progress['status']='completed'
        except InterruptedError:self.progress['status']='cancelled'
        except Exception as error:
            self.progress.update(status='failed',error='Report preparation stopped. '+str(error)[:700]+' Saved evidence and completed batches are retained.')
        self.progress['finished_at']=datetime.now(timezone.utc).isoformat();self.state['status']='report_'+self.progress['status'];self.publish(self.state)
        save(self.root/'summary.json',self.progress,'')
