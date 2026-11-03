"""Keep a record of the application's AI use so it can be inspected and counted.

Counts token useage and model calls.

Every research step can make requests to the AI service, including requests that
fail or return unusable answers. model_client.py supplies the details of each
attempt; this file saves them and adds up known usage for the small Usage panel
shown on each page. It can also read usage from older saved response files.

A received reply, an accepted result and a known usage count are different things.
Keeping them separate prevents failed work from disappearing from the accounting.
Missing usage stays unknown. This file does not calculate a bill or enforce a
spending limit.
"""
from datetime import datetime, timezone
import json
from pathlib import Path

TOKEN_FIELDS=('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens')


def now():
    """Read the UTC wall clock for an auditable event timestamp.

    Args: None.
    Returns: ISO8601 timestamp; not a duration estimate.
    """
    return datetime.now(timezone.utc).isoformat()


def stage_group(label):
    """Map exact receipt labels to readable workflow stages.

    Args: label: Original model transport stage label.
    Returns: Stable stage group; unknown labels remain visible rather than omitted.
    """
    for prefix,group in [('link-ranking-','Link ranking'),('extract-','Extraction'),('evidence-','Selection'),('triage-','Triage'),('investigate-','Investigation'),('report-','Report')]:
        if label.startswith(prefix):return group
    return {'define':'Definition','clarify':'Clarification','refine':'Refinement','search_queries':'Search preparation'}.get(label,label)


def write_entry(root, entry):
    """Save one attempt's accounting metadata without exposing its full receipt.
    
    Atomic replacement lets the UI read either the previous complete row or the
    new complete row while a request is running. The caller must supply safe
    metadata: this function does not inspect or redact arbitrary entry contents.
    
    Args:
        root: Path to the owning session directory, which must already exist.
        entry: JSON-serializable metadata with a call_id suitable for a filename;
            must not contain credentials, prompts or raw source material.
    Returns:
        None. Creates model-ledger if needed and replaces this call's private JSON
        file through a temporary file. Filesystem/serialization errors propagate;
        the function does not send a model request or calculate token usage.
    """
    folder=root/'model-ledger';folder.mkdir(exist_ok=True,mode=0o700)
    path=folder/(entry['call_id']+'.json');temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(entry,indent=2));temporary.chmod(0o600);temporary.replace(path)


def mark_validation(caller, stage, session_id, accepted):
    """Record application validation independently from receiving a paid response.

    Args: caller: Model transport or test double; stage: Exact call label;
        session_id: Owning run; accepted: Whether output passed its stage checks.
    Returns: None; no-op for transports without ledger support. Never claims
        semantic truth; acceptance means only the stage's existing checks passed.
    """
    if hasattr(caller,'mark_validation'):caller.mark_validation(stage,session_id,accepted)


def checked_call(caller, stage, request, session_id, validate):
    """Send one request and record whether its ordinary validator accepted output.

    Args: caller: Transport; stage: Label; request: Unmodified provider payload;
        session_id: Run ID; validate: Function consuming returned text.
    Returns: Validator result. Exceptions propagate after recording rejection;
        no retries, schema changes or response repair are performed.
    """
    try:
        result=validate(caller(stage,request,session_id))
    except Exception:
        mark_validation(caller,stage,session_id,False);raise
    mark_validation(caller,stage,session_id,True)
    return result


def read_ledger(root, active=False):
    """Assemble the UI's usage view without changing historical evidence.
    
    New ledger rows take precedence over receipts for the same call. Older
    receipts can recover provider-reported usage but cannot supply validation
    or timing that was never recorded. Unknown usage must remain visible so
    known token subtotals are not mistaken for a complete bill.
    
    Args:
        root: Path to one session directory containing model-ledger and receipts.
        active: Whether work is currently active for this session. If false,
            unfinished ledger rows are displayed as interrupted, not still running.
    Returns:
        A dictionary of call rows, per-stage totals, overall known token subtotals
        and a scope explanation. Input, output and cache tokens are summed;
        reported thinking is already within output and is not added twice.
        This reads files only, without rewriting old rows or calling a provider.
        Malformed JSON or filesystem errors propagate to the caller.
    """
    rows=[]
    for path in sorted((root/'model-ledger').glob('*.json')):
        row=json.loads(path.read_text())
        if row['outcome']=='running' and not active:row['outcome']='interrupted'
        rows.append(row)
    seen={r['call_id'] for r in rows}
    for path in sorted(root.glob('*.json')):
        data=json.loads(path.read_text())
        if data.get('call_id') in seen:continue
        request=data.get('request',{})
        if not isinstance(request,dict) or 'model' not in request or not ('response' in data or 'error' in data):continue
        label=path.stem.rsplit('-',1)[0];response=data.get('response') or {}
        rows.append({'call_id':'legacy:'+path.stem,'stage':label,'stage_group':stage_group(label),
            'model':response.get('model',request['model']),'started_at':None,'finished_at':None,'duration_seconds':None,
            'attempted':True,'outcome':('incomplete' if response.get('stop_reason') and response['stop_reason']!='end_turn' else 'response_received') if response else 'failed','validation':'unknown',
            'provider_request_id':None,'response_id':response.get('id'),'stop_reason':response.get('stop_reason'),'receipt':path.name,
            'usage':response.get('usage'),'legacy':True,'max_output_tokens':request.get('max_tokens')})
    totals={};groups={}
    for name,items in [('total',rows),*[(g,[r for r in rows if r['stage_group']==g]) for g in sorted({r['stage_group'] for r in rows})]]:
        counts={'calls':len(items),'attempted':sum(bool(r['attempted']) for r in items),
            'responses':sum(bool(r.get('response_id')) or r['outcome']=='response_received' for r in items),
            'failed':sum(r['outcome'] in ('failed','incomplete','interrupted','not_sent') for r in items),
            'unknown_usage':sum(bool(r['attempted']) and any(not isinstance((r.get('usage') or {}).get(k),int) for k in ('input_tokens','output_tokens')) for r in items),
            'validated':sum(r.get('validation')=='accepted' for r in items),
            'rejected':sum(r.get('validation')=='rejected' for r in items),
            'validation_unknown':sum(r.get('validation')=='unknown' for r in items),
            'thinking_tokens':sum(((r.get('usage') or {}).get('output_tokens_details') or {}).get('thinking_tokens') or 0 for r in items)}
        for key in TOKEN_FIELDS:
            counts[key]=sum((r.get('usage') or {}).get(key) or 0 for r in items)
        counts['known_tokens']=sum(counts[k] for k in TOKEN_FIELDS)
        if name=='total':totals=counts
        else:groups[name]=counts
    return {'totals':totals,'stages':groups,'calls':sorted(rows,key=lambda r:r.get('started_at') or ''),
        'scope':'Known provider-reported token subtotals. Unknown usage is excluded, not assumed zero. Validation is structural/source-reference checking, not truth. Historical validation/timing may be unavailable.'}
