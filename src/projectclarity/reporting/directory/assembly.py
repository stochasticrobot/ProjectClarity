"""Combine answer contributions in code and retain their supporting facts.

Exact normalized labels are grouped; supplied distinguishing details keep homonyms
separate. No fuzzy alias matching or model merge is performed. Different field
values remain visible rather than being silently selected or rewritten.
"""
from collections import defaultdict


def text_key(value):
    """Normalize case and whitespace for literal comparison, not semantic identity.

    Args: value: Item label, distinguishing detail, or field value.
    Returns: Comparable text; punctuation and numbers retain their meaning.
    """
    return ' '.join(value.casefold().split())


def assemble_items(fragments, packet):
    """Produce answer entries and exclusions without any further model requests.

    Args:
        fragments: Checked, input-ordered batch outputs with private item tags.
        packet: Full frozen answer inputs.
    Returns:
        Report-compatible answer with no narrative. Exact names group only where
        distinguishing details agree; unqualified names stay separate if multiple
        identities exist. Different field values are retained as alternatives,
        conservatively marked conflicting. Explicitly excluded items are omitted
        unless their group also contains positive supported qualifications, in
        which case uncertainty stays visible. Every fact remains accounted for.
    """
    groups=defaultdict(list);excluded={r['fact_id']:r for out in fragments for r in out['exclusions']}
    by_label=defaultdict(list)
    for out in fragments:
        for entry in out['entries']:by_label[text_key(entry['label'])].append(entry)
    for label,entries in by_label.items():
        details={text_key(e['_item']['distinguishing_detail']) for e in entries if e['_item']['distinguishing_detail'].strip()}
        for entry in entries:
            detail=text_key(entry['_item']['distinguishing_detail'])
            if not detail and len(details)==1:detail=next(iter(details))
            groups[(label,detail)].append(entry)
    assembled=[]
    for (label,detail),entries in groups.items():
        candidates=[e for e in entries if e['_item']['disposition']=='candidate']
        rejected=[e for e in entries if e['_item']['disposition']=='excluded']
        all_ids={fid for e in entries for fid in e['label_fact_ids']}|{fid for e in entries for f in e['fields'] for fid in f['fact_ids']}
        positive_complete=all(any(f['requirement_id']==r['id'] and f['status']=='supported' for e in candidates for f in e['fields']) for r in packet['requirements'])
        if rejected and not positive_complete:
            # A negative qualification cannot be cancelled by merely knowing a
            # name or product. Keep all supporting/rejecting evidence in the audit.
            for fid in all_ids:excluded[fid]={'fact_id':fid,'reason':'Evidence explicitly rules this item out of the requested answer.'}
            continue
        item={'label':entries[0]['label'],'label_fact_ids':sorted(all_ids if rejected else {fid for e in entries for fid in e['label_fact_ids']}),'fields':[]}
        if detail:item['label']+=' — '+entries[0]['_item']['distinguishing_detail'] if entries[0]['_item']['distinguishing_detail'].strip() else ' — '+detail
        for requirement in packet['requirements']:
            fields=[f for e in candidates for f in e['fields'] if f['requirement_id']==requirement['id'] and f['value'].strip()]
            values={}
            for field in fields:values.setdefault(text_key(field['value']),field['value'].strip())
            status='unknown' if not values else 'conflicting' if len(values)>1 or any(f['status']=='conflicting' for f in fields) else 'supported' if any(f['status']=='supported' for f in fields) else 'insufficient'
            if rejected and values:status='conflicting'
            item['fields'].append({'requirement_id':requirement['id'],'status':status,'value':' | '.join(values.values()),'fact_ids':sorted({fid for f in fields for fid in f['fact_ids']})})
        keys=sorted({e['_item']['order_key'].strip() for e in entries if e['_item']['order_key'].strip()})
        assembled.append((keys[0] if len(keys)==1 else '',label,detail,item))
    assembled.sort(key=lambda row:(not bool(row[0]),row[0],row[1],row[2]))
    entries=[row[3] for row in assembled]
    used={fid for e in entries for fid in e['label_fact_ids']}|{fid for e in entries for f in e['fields'] for fid in f['fact_ids']}
    return {'summary':[],'entries':entries,'gaps':[],'exclusions':[r for fid,r in sorted(excluded.items()) if fid not in used]}
