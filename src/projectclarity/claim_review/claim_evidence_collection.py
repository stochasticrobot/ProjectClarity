"""Gathers information that could help resolve ambiguities or missing
knowledge for a selected claim. 

Triage may find that a claim's quote omits a date, location or other needed detail.
Before asking the AI to review that problem, claim_investigation.py uses this file
to collect likely evidence from documents already saved for this research question.
It starts with the claim's original passage and nearby text, then looks for the
subject's name and words related to the claim and its missing information.

The result is a collection of source passages, called an evidence packet, with
source locations and reasons they were included. A size allowance limits the text
sent for review, and omitted candidates are recorded. Finding matching words is
only a way to find possible evidence: this file neither decides the claim is
supported nor searches the web for additional material.
"""
import hashlib
import json
from pathlib import Path
import re

from projectclarity.evidence_selection.relevance_selection import prepare_knowledge

STOP_WORDS = set('a an the and or but if of to in on at for from by with is are was were be been it its this that these those as has have had not no claim fact quote evidence source missing omitted information support supported unsupported review check whether does do did'.split())
GAP_TERMS = (
    {'headquarters','headquartered','head','office','based'},
    {'location','located','country','city','canada','canadian','operates','operations'},
    {'date','dated','year','month','when','time','period'},
    {'role','position','director','president','founder','chief'},
    {'quantity','amount','number','total','million','billion','percent'},
    {'attribution','attributed','speaker','said','reported','announced','according'},
    {'negation','denied','deny','never','not','no'},
)


def terms(text):
    """Normalize words for whole-token keyword matching, not identity resolution.

    Args: text: Claim, reason, entity name or source text.
    Returns: Lowercase Unicode word tokens of at least two characters.
    """
    return set(re.findall(r'[^\W_]{2,}', text.casefold()))


def checked_path(root, relative):
    """Constrain stored artifact references to their owning directory.

    Args: root: Owning artifact directory; relative: Stored relative path.
    Returns: Resolved safe path; raises ValueError for an escaping reference.
    """
    path=(root/relative).resolve()
    if not path.is_relative_to(root.resolve()):raise ValueError('Artifact outside owning run.')
    return path


class EvidenceCollector:
    """Snapshot all available saved text and accepted facts for one research run."""
    def __init__(self, root, state, cancelled):
        """Load and validate the run-local corpus without filtering by relevance.

        Args:
            root: Session directory.
            state: Saved definition and extraction run snapshot.
            cancelled: Callback raising InterruptedError when work must stop.
        Returns:
            None; builds passage/fact indexes in memory. Invalid unrelated sources
            are excluded explicitly; an invalid original claim later fails review.
        """
        self.root=checked_path(root,state['run']['artifact_directory'])
        self.passages={};self.originals={};self.errors=[];self.cancelled=cancelled
        index=json.loads((self.root/'knowledge.json').read_text())
        if index['research_question']!=state['research_question'] or index['information_needed']!=state['information_needed']:
            raise ValueError('Knowledge context differs from research definition.')
        for source in state['run'].get('sources',[]):
            cancelled()
            if not source.get('parsed'):continue
            try:
                text=(self.root/f'document-{source["id"]:03d}'/'document.txt').read_text()
                digest=hashlib.sha256(text.encode()).hexdigest()
                if digest!=source['text_sha256']:raise ValueError('Saved document hash changed.')
                for chunk in source['chunks']:
                    start,end=chunk['start'],chunk['end']
                    if not 0<=start<end<=len(text):raise ValueError('Invalid saved text offsets.')
                    key=(source['id'],chunk['index'])
                    self.passages[key]={'passage_id':f'D{key[0]}C{key[1]}','document_id':key[0],
                        'chunk_index':key[1],'source_url':source.get('final_url',source['url']),
                        'start_char':start,'end_char':end,'source_sha256':digest,
                        'pages':chunk.get('pages',[]),'text':text[start:end]}
            except Exception as error:
                self.errors.append({'document_id':source['id'],'reason':str(error)})
                self.passages={k:v for k,v in self.passages.items() if k[0]!=source['id']}
        self.knowledge_by_key={}
        for ref in index['chunks']:
            cancelled()
            try:
                raw=checked_path(self.root,ref['artifact']).read_bytes()
                if hashlib.sha256(raw).hexdigest()!=ref['sha256']:raise ValueError('Extraction hash changed.')
                original=json.loads(raw);knowledge=prepare_knowledge(original,ref['sha256'])
                text=json.loads(original['request']['messages'][0]['content'])['document_chunk']
                key=(ref['document_id'],ref['chunk_index'])
                # Receipt-only historical inputs remain usable, with the limitation disclosed.
                if key not in self.passages:
                    if any(e.get('document_id')==key[0] for e in self.errors):raise ValueError('Original document unavailable or changed.')
                    self.passages[key]={'passage_id':f'D{key[0]}C{key[1]}','document_id':key[0],
                        'chunk_index':key[1],'source_url':ref['source_url'],'pages':original.get('pages',[]),
                        'source_sha256':ref['sha256'],'text':text,'receipt_only':True}
                if self.passages[key]['text']!=text:raise ValueError('Receipt text differs from saved document chunk.')
                self.originals[ref['artifact']]={'reference':ref,'knowledge':knowledge,'key':key}
                self.knowledge_by_key[key]=knowledge
            except Exception as error:
                self.errors.append({'artifact':ref['artifact'],'reason':str(error)})

    def collect(self, record, budget=30000):
        """Gather likely supporting passages for one claim's specific review problem.
        
        Always try the original passage first, then adjacent chunks, then passages
        matching the subject's name and claim/review words. This simple ranking makes
        nearby missing context available without asking a model to search. Cross-source
        name matches are candidates, not proof that two entities are identical.
        
        Args:
            record: Selected original fact with fact_id, source artifact/hash, original
                fact index, entities and triage_reason describing the missing support.
            budget: Maximum total passage-text characters, default 30000. This is not
                a token or whole-request limit; passages are included whole or omitted.
        Returns:
            An evidence packet containing the unchanged claim, exact saved passages,
            source references and retrieval diagnostics (keywords, counts, omissions
            and source errors). No files are changed and no network/model calls occur.
            Raises if the original fact differs from its saved extraction, its source
            is unavailable, or the original passage cannot fit. The cancellation
            callback can interrupt scanning. Other candidates that exceed the budget
            are recorded as omitted rather than silently truncated.
        """
        self.cancelled()
        source=record['source'];origin=self.originals.get(source['artifact'])
        if not origin or origin['reference']['sha256']!=source['sha256']:raise ValueError('Original extraction unavailable or changed.')
        matches={f['fact_id']:f for f in origin['knowledge']['facts']}
        fact=matches.get(record['fact_id'])
        if not fact or fact['fact']!=record['fact'] or fact['source_fact_index']!=record['source_fact_index']:
            raise ValueError('Triaged claim differs from original extraction.')
        subject=record['fact']['subject_id']
        # Schema has no canonical alias field; use only existing names, not invented aliases.
        names=[e['name'] for e in origin['knowledge']['entities'] if e['id']==subject]
        normalized=[' '.join(re.findall(r'[^\W_]+',n.casefold())) for n in names]
        gap=terms(record['triage_reason'])-STOP_WORDS
        expanded=set(gap)
        for group in GAP_TERMS:
            if gap & group:expanded.update(group)
        keywords=(terms(record['fact']['statement'])-STOP_WORDS)|expanded
        key=origin['key'];candidates=[]
        for candidate,passage in self.passages.items():
            self.cancelled()
            text=passage['text'];words=terms(text)
            joined=' '+' '.join(re.findall(r'[^\W_]+',text.casefold()))+' '
            subject_match=any(' '+name+' ' in joined for name in normalized if name)
            knowledge=self.knowledge_by_key.get(candidate,{'entities':[],'facts':[]})
            subject_ids={e['id'] for e in knowledge['entities'] if
                         ' '.join(re.findall(r'[^\W_]+',e['name'].casefold())) in normalized}
            if candidate==key:subject_ids={subject}
            subject_facts=[f for f in knowledge['facts'] if f['fact']['subject_id'] in subject_ids]
            fact_matches=terms(' '.join(f['fact']['statement']+' '+f['fact']['predicate']+' '+f['fact']['value'] for f in subject_facts)) & keywords
            subject_match=subject_match or bool(subject_facts)
            passage={**passage,'matched_fact_ids':[f['fact_id'] for f in subject_facts if terms(f['fact']['statement']+' '+f['fact']['predicate']+' '+f['fact']['value']) & keywords]}
            score=len(words & keywords)+2*len(words & expanded)+len(fact_matches)
            if candidate==key:priority=0;reason='Original passage'
            elif candidate[0]==key[0] and abs(candidate[1]-key[1])==1:priority=1;reason='Neighboring passage'
            elif subject_match and ((words & keywords) or fact_matches):
                priority=2 if candidate[0]==key[0] else 3
                reason='Subject name and claim/review terms'+(' in extracted facts and text' if fact_matches else ' in saved text')
            else:continue
            candidates.append((priority,-score,candidate,reason,passage))
        candidates.sort(key=lambda c:(c[0],c[1],c[2]))
        selected=[];used=0;omitted=[];seen=set()
        for priority,score,candidate,reason,passage in candidates:
            digest=hashlib.sha256(passage['text'].encode()).hexdigest()
            # Keep original/neighbor positions; deduplicate repeated later source text.
            if digest in seen:continue
            seen.add(digest)
            if used+len(passage['text'])>budget:
                if priority==0:raise ValueError('Original passage exceeds evidence budget.')
                omitted.append(passage['passage_id']);continue
            selected.append({**passage,'retrieval_reason':reason});used+=len(passage['text'])
        if not selected:raise ValueError('No original source passage available.')
        return {'claim':record,'passages':selected,'retrieval':{'subject_names':names,
            'keywords':sorted(keywords),'gap_terms':sorted(expanded),'character_budget':budget,
            'characters_included':used,'passages_searched':len(self.passages),
            'facts_searched':sum(len(k['facts']) for k in self.knowledge_by_key.values()),
            'documents_searched':[{'document_id':doc,'source_url':url} for doc,url in sorted({(p['document_id'],p['source_url']) for p in self.passages.values()})],
            'candidates_found':len(candidates),
            'passages_included':len(selected),'omitted_passage_ids':omitted,'source_errors':self.errors,
            'receipt_only_passages':sum(bool(p.get('receipt_only')) for p in self.passages.values()),
            'scope':'Current run only. Name matches are candidate identities, not resolved entities.'}}
