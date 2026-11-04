"""Read supplied document paths and turn their contents into saved facts.

The paths may have come from search, a person or another part of the application.
This file does not know or care how they were found. It uses document_pool.py to
prepare the supplied list, asks a reader for each document, saves its contents and
sends readable text to knowledge extraction. It records progress and failures so
the user can inspect what was read and what information was obtained.

The current web reader handles URLs and checks website access rules. The pool can
hold other kinds of paths, but local-file reading is not enabled by this separation.
Search runs separately in search_execution.py; it is not a prerequisite for using
this document processor with an existing list of paths.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from queue import Queue, Empty
from projectclarity.knowledge_extraction.concurrent_extraction import extract_chunk
from projectclarity.knowledge_extraction.adaptive_chunk_sizing import AdaptiveChunkSizing, CHUNK_SIZES


from projectclarity.documents.web_acquisition import RobotsReader, SourceError, parse_document, split_text, canonical_url, read_document_text
from projectclarity.documents.document_pool import DocumentPool
from projectclarity.knowledge_extraction.knowledge_extraction import save




class DocumentPipeline:
    """Bounded orchestrator with injectable external I/O and real per-item progress."""
    def __init__(self, root, state, publish, caller, stop, paths=(), reader=None):
        """Create document processing from supplied paths, without performing search.

        Args:
            root: Owning session directory; a new private attempt folder is created.
            state: Saved session identity, question and answer requirements. No
                prepared queries, search provider or search results are required.
            publish: Callback saving public progress after each work unit.
            caller: Model transport used for extraction and optional fact repair.
            stop: Event requesting cancellation between work units.
            paths: Iterable of path strings or entries accepted by DocumentPool.
                The current default reader supports web URLs, not local-file reading.
            reader: Optional reader implementing fetch(path), for another source
                format or for offline tests. Pool entries are independent of it.
        Returns:
            None. Creates private progress and input files without network/model
            calls. Existing attempts are never overwritten. Invalid inputs raise.
        """
        self.root=root/('run-'+uuid4().hex[:12]);self.root.mkdir(mode=0o700)
        self.state,self.publish,self.caller,self.stop=state,publish,caller,stop
        self.run={'artifact_directory':self.root.name,'status':'running','stage':'downloading',
                  'events':[],'sources':[], 'model_calls':0,'chunks_total':0,'chunks_finished':0,
                  'chunks_passed':0,'chunks_partial':0,'chunks_split':0,'recovery_calls':0,'facts':0,'facts_unresolved':0,
                  'facts_withdrawn':0,'facts_awaiting_repair':0,
                  'limits':{'document_bytes':3000000,'document_characters':None,
                            'chunk_characters':3000,'extraction_chunk_ladder':list(CHUNK_SIZES),
                            'extraction_promotion_successes':2,'extraction_promotion_output_fraction':0.60,
                            'extraction_concurrency':state.get('extraction_concurrency',6),'robots_policy_version':2,
                            'robots_max_attempts':3,'robots_max_retry_wait_seconds':30},
                  'started_at':datetime.now(timezone.utc).isoformat()}
        self.state['run']=self.run
        self.reader=reader or RobotsReader(self.root,self.cancelled,self.event)
        self.set_paths(paths)

    def set_paths(self, paths):
        """Prepare supplied paths for reading before document processing starts.

        Args:
            paths: Strings or entries containing path, optional title and optional
                provenance. Provenance is retained as supplied, not interpreted.
        Returns:
            None. Replaces the unstarted pool, assigns document numbers and saves
            its input manifest. Duplicate exact paths share one processing record.
            Raises if work has already begun; never discovers additional paths.
        """
        if self.run['status']!='running' or any(source['status']!='queued' for source in self.run['sources']):
            raise ValueError('Cannot replace the document pool after processing starts.')
        pool=DocumentPool(paths)
        self.run['sources']=[{'id':i+1,'url':entry['path'],'title':entry['title'],
            'provenance':entry['provenance'],'status':'queued','downloaded':False,
            'parsed':False,'chunks':[],'facts':0} for i,entry in enumerate(pool.entries)]
        save(self.root/'manifest.tmp',{'research_question':self.state['research_question'],
             'information_needed':self.state['information_needed'],'paths':pool.entries,
             'limits':{k:v for k,v in self.run['limits'].items() if k in
                       ('document_bytes','document_characters','chunk_characters','extraction_chunk_ladder',
                        'extraction_promotion_successes','extraction_promotion_output_fraction','extraction_concurrency','robots_policy_version','robots_max_attempts','robots_max_retry_wait_seconds')},
             'user_agent':'ClarityResearchBot/0.1',
             'prompt_sha256':hashlib.sha256((Path(__file__).resolve().parents[1]/'prompts/structured_information_extraction.txt').read_bytes()).hexdigest()},'')
        (self.root/'manifest.tmp').replace(self.root/'manifest.json')

    def cancelled(self):
        """Stop scheduling work when the user requests cancellation.

        Args: None.
        Returns: None; raises InterruptedError when stopped. An in-flight provider
            request may finish first; its receipt remains saved and is not retried.
        """
        if self.stop.is_set():
            raise InterruptedError('Stopped by user.')

    def event(self, message):
        """Persist each progress transition and publish a bounded live event tail.

        Args:
            message: Human-readable operational update, never provider credentials.
        Returns:
            None. Appends to private events.jsonl and updates the UI snapshot.
        """
        entry={'time':datetime.now(timezone.utc).isoformat(),'message':message}
        self.run['events'].append(entry)
        self.run['events']=self.run['events'][-100:]
        path=self.root/'events.jsonl'
        with path.open('a') as stream:
            path.chmod(0o600);stream.write(json.dumps(entry)+'\n')
        self.publish(self.state)


    def acquire(self, graph_state):
        """Exhaust the discovered pool once, saving complete bytes before parsing.

        Args:
            graph_state: Session identity passed by the graph.
        Returns:
            Unchanged state. Every source receives an acquired, blocked or failed
            outcome. Parsed text is saved whole with original line endings, or
            rejected explicitly, never trimmed.
            In linked rounds, a redirect to a collected destination retains its
            acquisition receipt but skips repeat extraction; the initial alias GET
            cannot be avoided when its redirect destination was not previously known.
        """
        self.run['stage']='downloading'
        known_destinations = set()
        for prior in self.state.get('round_history', []):
            for previous in prior.get('run', {}).get('sources', []):
                if previous.get('downloaded'):
                    known_destinations.add(canonical_url(previous.get('final_url') or previous['url']))
        for source in self.run['sources']:
            self.cancelled()
            source['status']='checking_robots'
            self.event(f'Checking source {source["id"]}/{len(self.run["sources"])}: '+source['url'])
            folder=self.root/f'document-{source["id"]:03d}';folder.mkdir(mode=0o700)
            try:
                final,headers,body,hops=self.reader.fetch(source['url'])
                source.update(downloaded=True,final_url=final,bytes=len(body),status='parsing')
                raw=folder/'source.bin';raw.write_bytes(body);raw.chmod(0o600)
                save(folder/'web_acquisition.json',{'original_url':source['url'],'final_url':final,
                    'content_type':headers.get('content-type',''),'hops':hops,
                    'sha256':hashlib.sha256(body).hexdigest(),'bytes':len(body)},'')
                # Unknown aliases can only be recognized after the reader resolves
                # redirects. Keep the receipt but never extract the same page again.
                destination = canonical_url(final)
                if self.state.get('current_round', 1) > 1 and destination in known_destinations:
                    source.update(status='already_downloaded', duplicate=True)
                    self.event(f'Source {source["id"]}: redirect destination already collected; extraction skipped.')
                    continue
                known_destinations.add(destination)
                parsed=parse_document(body,headers.get('content-type',''))
                text,parser=parsed['text'],parsed['parser']
                save(folder/'parsing.json',{k:v for k,v in parsed.items() if k!='text'},'')
                path=folder/'document.txt';path.write_text(text, encoding='utf-8', newline='');path.chmod(0o600)
                source.update(status='downloaded',parsed=True,characters=len(text),parser=parser,pages=parsed['pages'],warnings=parsed['warnings'],
                              text_sha256=hashlib.sha256(text.encode()).hexdigest())
                chunks=split_text(text)
                offset=0
                for index,chunk in enumerate(chunks):
                    source['chunks'].append({'index':index,'start':offset,'end':offset+len(chunk),'pages':[p['page'] for p in parsed['pages'] if p['has_text'] and p['start'] < offset+len(chunk) and p['end'] > offset],'status':'pending','facts':0})
                    offset+=len(chunk)
                self.run['chunks_total']+=len(chunks)
                self.event(f'Downloaded source {source["id"]}: {len(text):,} text characters, {len(chunks)} chunks.')
            except InterruptedError:
                raise
            except Exception as error:
                code=error.code if isinstance(error,SourceError) else 'download_failed'
                source.update(status=code,error=str(error)[:1000])
                if isinstance(error,SourceError) and error.details:
                    source.update(pages=error.details.get('pages',[]),warnings=error.details.get('warnings',[]))
                save(folder/'failure.json',{'status':code,'error':source['error'],'url':source['url'],'details':getattr(error,'details',None)},'')
                self.event(f'Source {source["id"]}: {code} — '+source['error'])
        return graph_state

    def write_knowledge(self, knowledge):
        """Publish the accepted chunk index after each attempt so cancellation loses no index.

        Args:
            knowledge: Ordered accepted chunk artifact references, not model rewrites.
        Returns:
            None. Atomically saves the current knowledge bucket while retaining all
            original per-chunk receipts. File errors propagate instead of claiming success.
        """
        data={'research_question':self.state['research_question'],
              'information_needed':self.state['information_needed'],'chunks':knowledge,
              'facts':self.run['facts'],'scope':'Generic extracted assertions; not relevance-selected or verified truths.'}
        temporary=self.root/'knowledge.tmp'
        temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2));temporary.chmod(0o600)
        temporary.replace(self.root/'knowledge.json')

    def extract(self, graph_state):
        """Extract chunks in bounded parallel slots while serializing saved progress.

        Args:
            graph_state: Owning graph identity; source text and settings are saved
                in this pipeline's manifest. Existing sessions default to six slots.
        Returns:
            Unchanged graph state. Only this coordinator writes shared counters,
            events and the deterministically ordered knowledge index. Workers own
            isolated receipts and one extraction/repair call at a time. On stop,
            no further chunks are scheduled; in-flight results are drained and
            saved before InterruptedError. Each document starts at 3000 characters;
            confirmed truncation splits down to 1500, 750, then 325, while two small
            successful replies can promote pending ordinary work one level. Failed
            parents remain auditable but only successful children enter knowledge.
            Attempt counts include split parents; totals adjust as pending work is
            repartitioned. Other failures never trigger splitting.
        """
        limit = self.run['limits']['extraction_concurrency']
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError('Extraction concurrency must be an integer from 1 to 16.')
        self.run['stage'] = 'extracting'
        self.event(f'Knowledge extraction: {self.run["chunks_total"]} chunks, up to {limit} concurrent calls.')
        knowledge, documents, lookup = [], [], {}
        for source in self.run['sources']:
            if not source['parsed']:
                continue
            folder = self.root / f'document-{source["id"]:03d}'
            text = read_document_text(folder/'document.txt')
            documents.append((source, AdaptiveChunkSizing(source, text), text, folder))
        self.write_knowledge(knowledge)
        inbox = Queue()

        def updates():
            """Apply worker notifications on the sole session-writing thread.

            Args: None; drains this stage's private inbox.
            Returns: None; updates exact dispatched-call and pending-repair counts.
                Publishing never overlaps another extraction state writer.
            """
            while True:
                try:
                    kind, document, index, values = inbox.get_nowait()
                except Empty:
                    break
                chunk = lookup[(document, index)]
                if kind == 'call':
                    self.run['model_calls'] += 1
                    if chunk.get('parent_chunk_index') is not None:
                        self.run['recovery_calls'] += 1
                else:
                    accepted, pending = values
                    chunk.update(accepted=accepted, awaiting_repair=pending)
                    if pending and not self.stop.is_set():
                        chunk['status'] = 'repairing'
                self.run['facts_awaiting_repair'] = sum(c.get('awaiting_repair', 0) for c in lookup.values())
                self.publish(self.state)

        active = {}
        with ThreadPoolExecutor(max_workers=limit, thread_name_prefix='clarity-extraction') as pool:
            while active or (any(policy.pending() for _, policy, _, _ in documents) and not self.stop.is_set()):
                while len(active) < limit and not self.stop.is_set():
                    chosen = next((item for item in documents if item[1].pending()), None)
                    if chosen is None:
                        break
                    source, policy, text, folder = chosen
                    chunk = policy.pending()
                    content = text[chunk['start']:chunk['end']]
                    lookup[(source['id'], chunk['index'])] = chunk
                    source['status'] = 'extracting'; chunk['status'] = 'extracting'
                    future = pool.submit(extract_chunk, dict(source), dict(chunk), content,
                        self.state['information_needed'], self.caller, self.state['session_id'], self.stop, inbox)
                    active[future] = (source, chunk, folder, policy)
                    self.event(f'Extracting source {source["id"]}, chunk {chunk["index"]+1} ({len(content)} characters).')
                if not active:
                    break
                done, _ = wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
                updates()
                for future in sorted(done, key=lambda f: (active[f][0]['id'], active[f][1]['start'])):
                    source, chunk, folder, policy = active.pop(future)
                    receipt = future.result()
                    # A worker can enqueue its last notification between wait and
                    # result collection. Drain it before clearing pending repairs.
                    updates()
                    chunk['awaiting_repair'] = 0
                    if receipt is None:
                        chunk['status'] = 'cancelled'
                        continue
                    if receipt['status'] in ('checks_pass', 'checks_partial'):
                        facts = len(receipt['parsed']['facts'])
                        unresolved, withdrawn = len(receipt['unresolved']), len(receipt['withdrawn'])
                        chunk.update(status=receipt['status'], facts=facts, accepted=facts,
                                     unresolved=unresolved, withdrawn=withdrawn)
                        source['facts'] += facts; self.run['facts'] += facts
                        self.run['facts_unresolved'] += unresolved
                        self.run['facts_withdrawn'] += withdrawn
                        self.run['chunks_partial' if unresolved else 'chunks_passed'] += 1
                    else:
                        chunk.update(status='failed', error=('Model output reached its token limit.' if receipt.get('output_truncated')
                                     else 'Extraction response failed or did not pass schema/source-quote checks.'))
                    previous_splits = sum(c['status'] == 'split' for c in source['chunks'])
                    policy_event = policy.observe(chunk, receipt, self.stop.is_set())
                    self.run['chunks_split'] += sum(c['status'] == 'split' for c in source['chunks']) - previous_splits
                    self.run['chunks_total'] = sum(len(s['chunks']) for s in self.run['sources'])
                    receipt['recovery'] = {'status': chunk['status'],
                        'source_span': [chunk['start'], chunk['end']],
                        'child_links_location': 'document-summary.json sources[].chunks',
                        'error': chunk.get('error')}
                    path = folder/f'chunk-{chunk["index"]:04d}.json'
                    save(path, receipt, '')
                    if receipt['status'] in ('checks_pass', 'checks_partial'):
                        knowledge.append({'source_url':source['final_url'], 'document_id':source['id'],
                            'chunk_index':chunk['index'], 'start_char':chunk['start'], 'pages':chunk.get('pages',[]),
                            'artifact':str(path.relative_to(self.root)),
                            'sha256':hashlib.sha256(path.read_bytes()).hexdigest(), 'facts':chunk['facts']})
                        knowledge.sort(key=lambda row: (row['document_id'], row['start_char'], row['chunk_index']))
                    self.run['chunks_finished'] += 1
                    self.run['facts_awaiting_repair'] = sum(c.get('awaiting_repair',0) for c in lookup.values())
                    if all(c['status'] not in ('pending','extracting','repairing') for c in source['chunks']):
                        source['status'] = 'extracted' if all(c['status'] in ('checks_pass', 'split') for c in source['chunks']) else 'extraction_partial'
                    self.write_knowledge(knowledge)
                    if policy_event:
                        self.event(f'Source {source["id"]}: {policy_event}')
                    self.event(f'Source {source["id"]}, chunk {chunk["index"]+1}: {chunk["status"]}, {chunk["facts"]} facts.')
        updates()
        for source in self.run['sources']:
            if source['parsed']:
                source['status'] = 'extracted' if all(c['status'] in ('checks_pass', 'split') for c in source['chunks']) else 'extraction_partial'
        self.run['facts_awaiting_repair'] = 0
        self.write_knowledge(knowledge)
        self.cancelled()
        return graph_state

    def execute(self):
        """Run the real sequential graph and publish a truthful terminal summary.

        Args: None.
        Returns: None. Saves completed/partial/failed/cancelled extraction status
            and all earlier work. If auto_select is enabled, holds the session's
            active status until the research graph starts selection, preventing
            a duplicate start at the handoff. Failed/stopped runs are not replayed.
        """
        # Imported at execution time to keep stage implementation independent of
        # graph construction during module loading.
        from projectclarity.workflow.research_graph import build_document_graph
        graph = build_document_graph(self)
        try:
            graph.invoke({'session_id':self.state['session_id']})
            complete=bool(self.run['sources']) and all(s['status'] in ('extracted', 'already_downloaded') for s in self.run['sources']) and not any(s.get('warnings') for s in self.run['sources'])
            self.run['status']='completed' if complete else ('partial' if self.run['chunks_passed'] or self.run['chunks_partial'] else 'failed')
        except InterruptedError:
            self.run['status']='cancelled'
        except Exception as error:
            self.run.update(status='failed',error=type(error).__name__+': '+str(error)[:500])
        self.run['stage']='finished'
        self.run['finished_at']=datetime.now(timezone.utc).isoformat()
        self.state['status']='run_'+self.run['status']
        if self.state.get('auto_select') and not self.stop.is_set() and self.run['status'] in ('completed','partial'):
            self.state['status']='run_running'
        save(self.root/'document-summary.json',self.run,'')
        self.event(f'Extraction {self.run["status"]}: {self.run["facts"]} facts in {self.run["chunks_passed"]}/{self.run["chunks_total"]} checked chunks.')
