"""Filter reviewed evidence into small answer items, not miniature narrative reports.

The model selects supported field values. Code checks reference integrity and
accounts for omitted facts. Irrelevant facts need only their IDs, not explanations.
"""
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
        Report-compatible fragments plus item tags and omission metadata. Unknown
        IDs and uncited values fail. Repeated field contributions are combined
        without losing distinct values or citations. Unused facts
        are explicit exclusions; omitted facts enter the existing bounded recovery.
    """
    result=SelectedItems.model_validate_json(text).model_dump()
    # Reuse the same exact/unique case-and-space matching for every ID list.
    result['fact_ids']=result.pop('unused_fact_ids')
    result=normalize_references(result,[f['id'] for f in facts],[r['id'] for r in packet['requirements']])
    unused=set(result.pop('fact_ids'));used=set();entries=[]
    for item in result['items']:
        if not item['label'].strip() or not item['fact_ids']:
            raise ValueError('Selected item needs a label and supporting facts.')
        fields={};references=set(item['fact_ids'])
        for field in item['fields']:
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
        '_recovery':{'missing_fact_ids':[f['id'] for f in facts if f['id'] not in used|unused],
                     'withheld_uncited_summary':[]}}
