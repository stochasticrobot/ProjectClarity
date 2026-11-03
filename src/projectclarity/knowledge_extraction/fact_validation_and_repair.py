"""Corrects errors related to LLM output related to information extraction.

Keep useful extracted facts when another fact from the same text is faulty.

An AI reply may contain several valid facts alongside a missing reference or a
quote that cannot be found. Discarding the whole reply would lose useful evidence.
The document pipeline uses this file to check each proposed fact separately, keep
the valid ones and record why the others were rejected.

It locates quotations in the original text, allowing differences in spacing but
not changes to words. When needed, it makes at most one further AI request to
correct or withdraw the rejected proposals. Corrections must pass the same checks;
unresolved proposals remain visible and accepted facts are not overwritten. This
cannot recover an incomplete reply or establish whether a claim is actually true.
"""
import json
import re
from collections import Counter
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict
from projectclarity.knowledge_extraction.knowledge_extraction import Entity, Fact, validate_response
from projectclarity.model_access.model_ledger import mark_validation


def normalized(text):
    """Collapse whitespace while retaining a map to original character spans.

    Args: text: Unmodified source or proposed quote.
    Returns: Normalized text and per-character (start,end) source positions.
        Punctuation, numbers and case remain unchanged.
    """
    pieces=[];positions=[]
    for match in re.finditer(r'\s+|\S',text):
        pieces.append(' ' if match.group().isspace() else match.group())
        positions.append(match.span())
    return ''.join(pieces),positions


def match_quote(quote, source):
    """Locate one source occurrence so an accepted quote can be traced exactly.
    
    Try literal matching first, then allow differences in whitespace only. The
    source span is retained even when the proposed quote used different spacing.
    Multiple occurrences are rejected because their position would be ambiguous.
    
    Args:
        quote: Proposed supporting text, not a filename or search expression.
        source: Exact original document chunk against which the quote is checked.
    Returns:
        A dictionary with the original source substring, zero-based start/end
        character offsets (end excluded) and matching method. Offsets are local to
        this chunk, not the whole document. Raises ValueError for blank, missing or
        ambiguous quotes. Does not change words, punctuation, numbers or case, and
        does not determine whether the passage supports the claim's meaning.
    """
    for method in ('exact','whitespace'):
        haystack,positions=(source,None) if method=='exact' else normalized(source)
        needle=quote if method=='exact' else normalized(quote)[0].strip()
        if not needle:raise ValueError('Blank quote')
        starts=[m.start() for m in re.finditer('(?='+re.escape(needle)+')',haystack)]
        if len(starts)>1:raise ValueError('Ambiguous source quote; include more context')
        if starts:
            a=starts[0];b=a+len(needle)
            if positions:a,b=positions[a][0],positions[b-1][1]
            return {'quote':source[a:b],'start':a,'end':b,'method':method}
    raise ValueError('Supporting quote not found in chunk')


def assess(text, source):
    """Check proposals separately so one invalid fact does not discard its neighbours.
    
    Invalid or duplicate entity definitions are excluded. Facts referring to
    those missing entities then fail individually, while independent facts can
    survive. Quote matching finds source text; it is not a semantic truth check.
    
    Args:
        text: Complete model response as JSON text with entities and facts lists.
        source: Exact source chunk supplied to extraction, used for quote matching.
    Returns:
        Accepted entities/facts under parsed, grounding records mapping accepted
        facts to source spans, rejected proposals with stable proposal IDs/reasons,
        and entity issues. Whitespace-only matches use the actual source substring
        in the accepted fact, retaining the proposed quote in its grounding record.
        Invalid JSON or the outer response structure raises; per-record errors are
        collected. No files, network calls or model repairs are performed here.
    """
    data=json.loads(text)
    if not isinstance(data,dict) or set(data)!={'entities','facts'} or not isinstance(data['entities'],list) or not isinstance(data['facts'],list):
        raise ValueError('Invalid extraction envelope')
    counts=Counter(e.get('id') for e in data['entities'] if isinstance(e,dict) and isinstance(e.get('id'),str))
    entities=[];issues=[]
    for raw in data['entities']:
        try:
            e=Entity.model_validate(raw)
            if counts[e.id]!=1 or not all(v.strip() for v in (e.id,e.name,e.type)):raise ValueError('Blank or duplicate entity ID/name/type')
            entities.append(e.model_dump())
        except Exception as error:issues.append({'entity':raw,'reason':str(error)[:500]})
    accepted=[];grounding=[];rejected=[]
    for index,raw in enumerate(data['facts']):
        try:
            fact=Fact.model_validate(raw).model_dump();match=match_quote(fact['quote'],source)
            fact['quote']=match['quote']
            validate_response(json.dumps({'entities':entities,'facts':[fact]}),source)
            grounding.append({'proposal_index':index,'accepted_index':len(accepted),'proposed_quote':raw['quote'],**match})
            accepted.append(fact)
        except Exception as error:rejected.append({'id':f'P{index+1}','fact':raw,'reason':str(error)[:500]})
    return {'parsed':{'entities':entities,'facts':accepted},'grounding':grounding,'rejected':rejected,'entity_issues':issues}


class RepairDecision(BaseModel):
    """One identified rejected proposal, corrected or explicitly withdrawn."""
    model_config=ConfigDict(extra='forbid',strict=True)
    id:str
    action:Literal["correct","withdraw"]
    reason:str
    fact:Fact|None


class Repairs(BaseModel):
    """Repairs may supply entities but cannot edit already accepted facts."""
    model_config=ConfigDict(extra='forbid',strict=True)
    entities:list[Entity]
    decisions:list[RepairDecision]


def recover(text, source, caller, sid, stage, stopped, progress):
    """Preserve valid facts and make at most one attempt to repair rejected ones.
    
    A single invalid quote should not cost the research run every other fact in
    its chunk. Repairs are checked against the same original text and cannot
    replace previously accepted records. This checks structure and quote presence,
    not whether the source actually establishes the proposed assertion.
    
    Args:
        text: Complete extraction response as JSON text, before any repair.
        source: Exact source chunk used for that extraction, not a URL or path.
        caller: Callable model transport accepting stage, request and session ID;
            it exposes a model name and may record validation in the ledger.
        sid: Owning session ID used to store the repair call's receipt.
        stage: Unique receipt label for this chunk's possible repair request.
        stopped: Zero-argument predicate returning true when no repair may start.
        progress: Callback receiving accepted-fact and rejected-proposal counts
            after the first assessment, before a possible repair call.
    Returns:
        A dictionary containing accepted entities/facts, source-span mappings,
        initial rejection reasons, unresolved proposals, withdrawals and a repair
        receipt (or None). Calls the model only when rejected proposals remain and
        stopped is false. Repair failures are recorded and leave proposals
        unresolved; they are not retried. Invalid initial JSON/envelopes raise
        before repair, and callback exceptions propagate.
    """
    first=assess(text,source);parsed=first['parsed'];grounding=list(first['grounding'])
    pending=first['rejected'];unresolved=list(pending);withdrawn=[];repair=None
    progress(len(parsed['facts']),len(pending))
    if pending and not stopped():
        schema=Repairs.model_json_schema()
        request={'model':caller.model,'max_tokens':8192,'system':(Path(__file__).resolve().parents[1]/'prompts/repair_extracted_facts.txt').read_text(),
                 'output_config':{'format':{'type':'json_schema','schema':schema}},
                 'messages':[{'role':'user','content':json.dumps({'document_chunk':source,'entities':parsed['entities'],
                    'entity_issues':first['entity_issues'],'rejected_facts':pending},ensure_ascii=False)}]}
        repair={'request':request,'status':'failed'}
        try:
            raw=caller(stage,request,sid);repair['raw_text']=raw;result=json.loads(raw)
            if not isinstance(result,dict) or set(result)!={'entities','decisions'} or not isinstance(result['entities'],list) or not isinstance(result['decisions'],list):raise ValueError('Invalid repair envelope')
            ids=[d.get('id') if isinstance(d,dict) else None for d in result['decisions']]
            if len(ids)!=len(set(ids)) or set(ids)!={f['id'] for f in pending}:raise ValueError('Repair must address each rejected ID exactly once')
            existing={e['id']:e for e in parsed['entities']};extra={}
            for item in result['entities']:
                entity=Entity.model_validate(item);value=entity.model_dump()
                if entity.id in extra or (entity.id in existing and existing[entity.id]!=value):raise ValueError('Repair changed or duplicated an entity')
                extra[entity.id]=value
            entities=list({**existing,**extra}.values());unresolved=[]
            for proposal in pending:
                item=next(d for d in result['decisions'] if d['id']==proposal['id'])
                try:decision=RepairDecision.model_validate(item)
                except Exception as error:
                    unresolved.append({**proposal,'repair_error':str(error)[:500]});continue
                if decision.action=='withdraw' and decision.fact is None and decision.reason.strip():
                    withdrawn.append({'id':decision.id,'reason':decision.reason});continue
                if decision.action!='correct' or decision.fact is None or not decision.reason.strip():
                    unresolved.append({**proposal,'repair_error':'Invalid correction/withdrawal decision'});continue
                checked=assess(json.dumps({'entities':entities,'facts':[decision.fact.model_dump()]}),source)
                if checked['rejected'] or checked['entity_issues']:
                    unresolved.append({**proposal,'repair_error':checked['rejected'] or checked['entity_issues']});continue
                fact=checked['parsed']['facts'][0]
                if fact not in parsed['facts']:
                    index=len(parsed['facts']);parsed['facts'].append(fact)
                    grounding.append({**checked['grounding'][0],'accepted_index':index,'repair_id':decision.id})
                    needed={fact['subject_id'],fact['object_id']}
                    for entity in checked['parsed']['entities']:
                        if entity['id'] in needed and entity['id'] not in {e['id'] for e in parsed['entities']}:parsed['entities'].append(entity)
            repair['status']='partial' if unresolved else 'checks_pass'
            mark_validation(caller,stage,sid,not unresolved)
        except Exception as error:
            unresolved=list(pending);repair['error']=str(error)[:1000];mark_validation(caller,stage,sid,False)
    return {'parsed':parsed,'grounding':grounding,'initial_rejections':pending,'entity_issues':first['entity_issues'],
            'unresolved':unresolved,'withdrawn':withdrawn,'repair':repair}
