"""Run prepared searches and return candidate document paths.

This is the search step, before document processing. It runs each prepared query,
records successes and failures, and can try Wikipedia once if the second search
fails or finds nothing. The output is a list of URLs with display titles and notes
about where each came from. It does not create document-processing records or read
the returned pages. source_collection.py passes these paths to the document pool.
"""
from datetime import datetime, timezone
import json
from projectclarity.knowledge_extraction.knowledge_extraction import save
from projectclarity.documents.web_acquisition import SourceError, canonical_url
from projectclarity.search.web_search import search_web, search_wikipedia, SEARCH_INTERVAL_SECONDS


class SearchExecution:
    """One finite search step, independently usable without a document processor."""

    def __init__(self, root, state, progress, publish, stop, search=search_web,
                 fallback=search_wikipedia):
        """Capture prepared queries and search settings without contacting a service.

        Args:
            root: Existing private attempt directory for search receipts.
            state: Saved question and prepared searches, used for progress publication.
            progress: Mutable progress dictionary shared with the current run screen.
                Search writes only its own search records, limits and activity here.
            publish: Callback saving the current public session snapshot.
            stop: Event requesting cancellation between calls or while waiting.
            search: Search service callable; replaceable for offline tests.
            fallback: Wikipedia callable for the second query's single fallback.
        Returns:
            None. Saves search inputs separately from the document manifest; makes
            no network requests. Filesystem errors propagate.
        """
        self.root,self.state,self.run,self.publish,self.stop=root,state,progress,publish,stop
        self.search,self.fallback=search,fallback
        self.complete=False
        self.run.update(provider=state.get('search_provider','duckduckgo'),searches=[],stage='searching')
        self.run['limits'].update(results_per_query=5,max_urls=10,search_interval_seconds=SEARCH_INTERVAL_SECONDS)
        save(root/'search-manifest.json',{'searches':state['searches'],
             'provider':self.run['provider'],'results_per_query':5},'')

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
            The second query gets one Wikipedia fallback on primary failure/empty results;
            both attempts are retained. Query/title/snippet data never substitutes
            for downloaded document evidence.
        """
        paths=[]
        for index,query in enumerate(self.state['searches']):
            self.cancelled()
            record={'query':query['query'],'status':'searching','results':0,'provider':self.run['provider']}
            self.run['searches'].append(record)
            self.event(f'Search {index+1}/2: '+query['query'])
            def show_wait(seconds):
                """Publish provider cooldown separately from an active search request.

                Args: seconds: Positive wait duration, or zero when dispatching.
                Returns: None; updates the current search status and event history.
                """
                record['status']='waiting' if seconds else 'searching'
                self.event(f'Waiting {seconds:.0f}s before the next search request.' if seconds else f'Sending search {index+1}/2.')
            try:
                try:
                    receipt=self.search(query['query'],5,self.cancelled,provider=self.run['provider'],on_wait=show_wait) if self.search is search_web else self.search(query['query'],5,self.cancelled)
                    if index == 1 and not receipt['results']:
                        raise SourceError('search_empty', 'Primary search returned no results.', receipt)
                except InterruptedError:
                    raise
                except Exception as primary_error:
                    if index != 1:
                        raise
                    primary={'query':query['query'],'provider':self.run['provider'],'error':str(primary_error)[:1000],
                             'diagnostic':getattr(primary_error,'details',None)}
                    record.update(primary_failure=primary,provider='wikipedia',status='searching',fallback=True)
                    save(self.root/f'search-{index+1}-primary-failed.json',primary,'')
                    self.event('Second search unavailable; trying Wikipedia once with the same query.')
                    receipt=self.fallback(query['query'],5,self.cancelled)
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

