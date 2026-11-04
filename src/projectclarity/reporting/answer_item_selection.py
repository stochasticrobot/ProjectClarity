"""Filter reviewed evidence into small answer items, not miniature narrative reports.

The model selects supported field values. Code checks reference integrity and
accounts for omitted facts. Irrelevant facts need only their IDs, not explanations.
"""
import json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict
from projectclarity.model_access.model_client import request_for
from projectclarity.reporting.report_payloads import compact_packet
from projectclarity.reporting.report_reference_normalization import normalize_references


class SelectedField(BaseModel):
    """A brief field value with the evidence actually supporting it."""
    model_config=ConfigDict(extra='forbid',strict=True)
    requirement_id:str
    value:str
    status:Literal['supported','insufficient','conflicting']
    fact_ids:list[str]


class SelectedItem(BaseModel):
    """One candidate or explicitly ruled-out item from this evidence batch."""
    model_config=ConfigDict(extra='forbid',strict=True)
    label:str
    distinguishing_detail:str
    order_key:str
    disposition:Literal['candidate','excluded']
    fact_ids:list[str]
    fields:list[SelectedField]


class SelectedItems(BaseModel):
    """Answer contributions plus references not useful to the requested fields."""
    model_config=ConfigDict(extra='forbid',strict=True)
    items:list[SelectedItem]
    unused_fact_ids:list[str]


def selection_request(packet, facts, model):
    """Build one compact filtering request with IDs constrained by the schema.

    Args:
        packet: Frozen question, requested fields and coverage.
        facts: Whole evidence facts for this request.
        model: Configured model name.
    Returns:
        Complete provider request combining the short answer contract and batch
        task. Enum choices bind references to this input; no API call is made.
    """
    request=request_for('prepare_report_batch.txt',compact_packet(packet,facts),SelectedItems,model)
    contract=(Path(__file__).resolve().parents[1]/'prompts/prepare_report.txt').read_text()
    request['system']=contract.strip()+'\n\n'+request['system'].strip()
    schema=request['output_config']['format']['schema'];ids=[f['id'] for f in facts]
    schema['properties']['unused_fact_ids']['items']['enum']=ids
    for name in ('SelectedField','SelectedItem'):
        schema['$defs'][name]['properties']['fact_ids']['items']['enum']=ids
    schema['$defs']['SelectedField']['properties']['requirement_id']['enum']=[r['id'] for r in packet['requirements']]
    request.update(max_tokens=16384,timeout=180)
    return request


def validate_selection(text, packet, facts):
    """Check selected item references and expose genuinely omitted inputs for recovery.

    Args:
        text: Original provider response, retained unchanged by the dispatcher.
        packet: Frozen answer requirements.
        facts: Exact facts offered to this task.
    Returns:
        Report-compatible fragments plus item tags and omission metadata.
        Invalid item identities fail. Invalid or uncited fields are withheld and
        recorded as retryable issues, never published as supported values. Uncited fields marked
        insufficient are withheld in the audit, not published or given invented
        citations; other cited fields survive. Repeated contributions are combined
        without losing distinct values or citations. Unused facts
        are explicit exclusions; omitted facts enter the existing bounded recovery.
    """
    raw=json.loads(text)
    if not isinstance(raw,dict) or not isinstance(raw.get('items'),list):
        raise ValueError('Expected an answer item list.')
    # Validate item identities separately from fields: one malformed/missing field
    # must not discard otherwise attributable items or their batch siblings.
    result=SelectedItems.model_validate({**raw,'items':[]}).model_dump()
    invalid=[]
    for raw_item in raw['items']:
        if not isinstance(raw_item,dict):
            raise ValueError('Expected an answer item object.')
        item=SelectedItem.model_validate({**raw_item,'fields':[]}).model_dump()
        raw_fields=raw_item.get('fields',[])
        if not isinstance(raw_fields,list):
            invalid.append({'label':item['label'],'error':'Fields were not a list.'})
            raw_fields=[]
        for raw_field in raw_fields:
            try:
                field=SelectedField.model_validate(raw_field).model_dump()
                field=normalize_references(field,[f['id'] for f in facts],
                    [r['id'] for r in packet['requirements']])
                if (not field['value'].strip() or not field['fact_ids']) and not (
                        field['status']=='insufficient' and not field['fact_ids']):
                    raise ValueError('Selected value needs text and supporting facts.')
                item['fields'].append(field)
            except ValueError as error:
                invalid.append({'label':item['label'],'field':raw_field,'error':str(error)[:500]})
        result['items'].append(item)
    # Reuse the same exact/unique case-and-space matching for every ID list.
    result['fact_ids']=result.pop('unused_fact_ids')
    result=normalize_references(result,[f['id'] for f in facts],[r['id'] for r in packet['requirements']])
    unused=set(result.pop('fact_ids'));used=set();entries=[];withheld=[]
    for item in result['items']:
        if not item['label'].strip() or not item['fact_ids']:
            raise ValueError('Selected item needs a label and supporting facts.')
        fields={};references=set(item['fact_ids'])
        for field in item['fields']:
            # Models sometimes describe a missing value despite the instruction to
            # omit unsupported fields. Rejecting the entire batch loses its useful
            # cited items. Withhold only explicitly insufficient, uncited fields;
            # never publish their prose or borrow the item's citations as support.
            if field['status']=='insufficient' and not field['fact_ids']:
                withheld.append({'label':item['label'], **field})
                continue
            if not field['value'].strip() or not field['fact_ids']:raise ValueError('Selected value needs supporting facts.')
            field['fact_ids']=list(dict.fromkeys(field['fact_ids']))
            prior=fields.get(field['requirement_id'])
            if prior:
                # Multiple answers to a requested field are contributions, not a
                # malformed response. Retain alternatives rather than choosing one.
                if ' '.join(prior['value'].casefold().split()) != ' '.join(field['value'].casefold().split()):
                    prior['value']+=' | '+field['value']
                    prior['status']='conflicting' if 'conflicting' in (prior['status'],field['status']) else 'supported' if prior['status']==field['status']=='supported' else 'insufficient'
                elif field['status']=='conflicting':
                    prior['status']='conflicting'
                elif prior['status']!='conflicting' and field['status']=='supported':
                    prior['status']='supported'
                prior['fact_ids']=list(dict.fromkeys(prior['fact_ids']+field['fact_ids']))
            else:
                fields[field['requirement_id']]=field
            references.update(field['fact_ids'])
        used.update(references)
        entries.append({'label':item['label'].strip(),'label_fact_ids':list(dict.fromkeys(item['fact_ids'])),
            'fields':[fields.get(r['id'],{'requirement_id':r['id'],'status':'unknown','value':'','fact_ids':[]}) for r in packet['requirements']],
            '_item':{key:item[key] for key in ('distinguishing_detail','order_key','disposition')}})
    unused-=used
    return {'summary':[],'entries':entries,'gaps':[],
        'exclusions':[{'fact_id':fid,'reason':'Not selected for the requested answer fields.'} for fid in sorted(unused)],
        **({'_issues':invalid} if invalid else {}),
        '_recovery':{'missing_fact_ids':[f['id'] for f in facts if f['id'] not in used|unused],
                     'withheld_uncited_summary':[],
                     **({'withheld_insufficient_fields':withheld} if withheld else {})}}
