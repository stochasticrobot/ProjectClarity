"""Run prepared searches and return candidate document paths.

This is the search step, before document processing. It runs each prepared query,
records every attempt, and tries Mwmbl, Bing RSS, then Wikipedia if the primary
search fails or finds nothing. It stops at the first usable result set. The output is a list of URLs with display titles and notes
about where each came from. It does not create document-processing records or read
the returned pages. source_collection.py passes these paths to the document pool.
"""
from datetime import datetime, timezone
import json
from projectclarity.knowledge_extraction.knowledge_extraction import save
from projectclarity.documents.web_acquisition import SourceError, canonical_url
from projectclarity.search.web_search import search_web, SEARCH_INTERVAL_SECONDS


class SearchExecution:
    """One finite search step, independently usable without a document processor."""

    def __init__(self, root, state, progress, publish, stop, search=search_web,
                 fallback=None):
        """Capture prepared queries and search settings without contacting a service.

        Args:
            root: Existing private attempt directory for search receipts.
            state: Saved question and prepared searches, used for progress publication.
            progress: Mutable progress dictionary shared with the current run screen.
                Search writes only its own search records, limits and activity here.
            publish: Callback saving the current public session snapshot.
            stop: Event requesting cancellation between calls or while waiting.
            search: Search service callable; replaceable for offline tests.
            fallback: Optional single-provider override for controlled tests/callers.
                None uses the production Mwmbl, Bing RSS, Wikipedia chain.
        Returns:
            None. Saves search inputs separately from the document manifest; makes
            no network requests. Records the actual prepared query count and a
            five-results-per-query URL bound. Filesystem errors propagate.
        """
        self.root,self.state,self.run,self.publish,self.stop=root,state,progress,publish,stop
        self.search,self.fallback=search,fallback
        self.on_challenge = None
        self.complete=False
        self.run.update(provider=state.get('search_provider','duckduckgo'),searches=[],stage='searching')
        self.run['limits'].update(query_count=len(state['searches']),results_per_query=5,max_urls=5*len(state['searches']),search_interval_seconds=SEARCH_INTERVAL_SECONDS)
        save(root/'search-manifest.json',{'searches':state['searches'],
             'provider':self.run['provider'],'query_count':len(state['searches']),'results_per_query':5},'')

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


    def execute(self):
        """Execute each prepared query once and build a deduplicated finite URL pool.

        Args:
            None. Prepared queries were captured when this search stage was created.
        Returns:
            Candidate paths with display titles and origin notes. Saves each search
            receipt; failures remain explicit. No documents are downloaded.
            Each provider is tried at most once per query, in configured order, until
            usable results arrive. HTTP/reported errors and empty or unusable results
            advance the chain; cancellation stops immediately. A configured human
            challenge handoff pauses DuckDuckGo before fallback. All attempts are saved.
            Query/title/snippet data never substitutes
            for downloaded document evidence.
        """
        paths=[]
        for index,query in enumerate(self.state['searches']):
            self.cancelled()
            record={'query':query['query'],'status':'searching','results':0,'provider':self.run['provider']}
            self.run['searches'].append(record)
            self.event(f'Search {index+1}/{len(self.state["searches"])}: '+query['query'])
            def show_wait(seconds):
                """Publish provider cooldown separately from an active search request.

                Args: seconds: Positive wait duration, or zero when dispatching.
                Returns: None; updates the current search status and event history.
                """
                record['status']='waiting' if seconds else 'searching'
                self.event(f'Waiting {seconds:.0f}s before the next search request.' if seconds else f'Sending search {index+1}/{len(self.state["searches"])}.')
            try:
                providers = [(self.run['provider'], self.search)]
                providers += ([('wikipedia', self.fallback)] if self.fallback is not None else
                              [(name, search_web) for name in ('mwmbl', 'bing_rss', 'wikipedia')])
                record['attempts'] = []
                for attempt_index, (provider, search) in enumerate(providers):
                    self.cancelled()
                    record.update(provider=provider, status='searching', fallback=attempt_index > 0)
                    attempt = {'provider': provider, 'status': 'searching', 'results': 0}
                    record['attempts'].append(attempt)
                    self.event(f'Search {index+1}: trying {provider}.')
                    try:
                        options = {'provider':provider, 'on_wait':show_wait}
                        if provider == 'duckduckgo' and self.on_challenge is not None:
                            options['on_challenge'] = self.on_challenge
                        receipt = (search(query['query'],5,self.cancelled,**options)
                                   if search is search_web else search(query['query'],5,self.cancelled))
                        if receipt.get('error') or receipt.get('errors') or receipt.get('success') is False:
                            raise SourceError('search_error', f'{provider} reported a search error.', receipt)
                        usable = []
                        for result in receipt['results'][:5]:
                            try:
                                url = canonical_url(result['url'])
                            except (ValueError, KeyError, TypeError):
                                record.setdefault('rejected_urls', []).append(str(result.get('url', ''))[:2048])
                                continue
                            usable.append({**result, 'url': url})
                        if not usable:
                            raise SourceError('search_empty', f'{provider} returned no usable results.', receipt)
                        receipt = {**receipt, 'provider': provider, 'results': usable}
                        attempt.update(status='completed', results=len(usable))
                        save(self.root/f'search-{index+1}-attempt-{attempt_index+1}.json',
                             {'query':query['query'], **receipt, 'status':'completed'}, '')
                        break
                    except InterruptedError:
                        raise
                    except Exception as error:
                        attempt.update(status='failed', error=str(error)[:1000],
                                       diagnostic=getattr(error, 'details', None))
                        save(self.root/f'search-{index+1}-attempt-{attempt_index+1}.json',
                             {'query':query['query'], **attempt}, '')
                        if attempt_index == 0:
                            primary = {'query':query['query'], **attempt}
                            record['primary_failure'] = primary
                            save(self.root/f'search-{index+1}-primary-failed.json', primary, '')
                        self.event(f'Search {index+1}: {provider} failed: '+attempt['error'])
                        if attempt_index == len(providers)-1:
                            raise
                save(self.root/f'search-{index+1}.json',{'query':query['query'],**receipt},'')
                record.update(status='completed',results=len(receipt['results']))
                for result in receipt['results']:
                    try:
                        url=canonical_url(result['url'])
                    except ValueError:
                        record.setdefault('rejected_urls',[]).append(result['url'])
                        continue
                    paths.append({'path':url,'title':result['title'],
                                  'provenance':{'query_index':index+1,'snippet':result['snippet']}})
                self.event(f'Search {index+1} returned {record["results"]} URLs.')
            except InterruptedError:
                raise
            except Exception as error:
                record.update(status='failed',error=str(error)[:1000])
                if isinstance(error,SourceError) and error.details:
                    record['diagnostic']=error.details
                save(self.root/f'search-{index+1}-failed.json',record,'')
                self.event(f'Search {index+1} failed: '+record['error'])
        self.complete=all(item['status']=='completed' and item['results']
                          for item in self.run['searches'])
        return paths

