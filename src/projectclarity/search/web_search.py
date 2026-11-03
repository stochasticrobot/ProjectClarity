"""Ask search services for useful document addresses, without downloading those documents.

The application supplies prepared search terms. This file sends them to the chosen
search service and returns page addresses, titles and short descriptions. It keeps
waiting and failure details so the user can see why a search did not succeed.
Search results are leads, not evidence: another part of the application must read
the actual documents. search_execution.py decides which prepared searches to run.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from threading import Lock
from urllib.parse import urlencode, quote
from bs4 import BeautifulSoup
from projectclarity.documents.web_acquisition import SourceError, http_get

SEARCH_INTERVAL_SECONDS = 10
_search_lock = Lock()
_search_finished = {}

def search_wikipedia(query, limit, cancelled, transport=http_get):
    """Search English Wikipedia once through its documented public search API.

    Args:
        query: Saved second search query, unchanged; no model-generated broadening.
        limit: Maximum article leads returned, normally five.
        cancelled: Cooperative cancellation callback.
        transport: Bounded single-hop HTTP transport, injectable for offline tests.
    Returns:
        Explicit Wikipedia receipt, including an honest empty result. HTTP/API or
        malformed replies raise SourceError; no retries or challenge bypasses.
        Article downloads still go through the ordinary robots-aware reader.
    """
    url = 'https://en.wikipedia.org/w/api.php?' + urlencode({
        'action':'query','list':'search','srsearch':query,'srlimit':limit,
        'srnamespace':0,'format':'json','utf8':1})
    cancelled()
    status, headers, body = transport(url, 1000000, cancelled)
    if status != 200:
        raise SourceError('search_error', f'Wikipedia search returned HTTP {status}.')
    try:
        data = json.loads(body)
        rows = data['query']['search']
        if not isinstance(rows, list):
            raise ValueError('Invalid search rows')
        results = [{'url':'https://en.wikipedia.org/wiki/'+quote(r['title'].replace(' ','_'),safe=''),
                    'title':r['title'], 'snippet':BeautifulSoup(r.get('snippet',''),'html.parser').get_text()} for r in rows[:limit]]
    except (KeyError, ValueError, TypeError) as error:
        raise SourceError('search_error','Wikipedia returned an invalid search response.') from error
    return {'provider':'wikipedia','http_status':status,'query':query,'results':results}


def search_web(query, limit, cancelled, provider="duckduckgo", on_wait=None):
    """Pace same-provider searches and preserve observed failure diagnostics.

    Args:
        query: Saved search query, unchanged.
        limit: Requested result count.
        cancelled: Callback that raises when scheduling should stop.
        provider: Explicit engine; no automatic fallback.
        on_wait: Optional callback receiving cooldown seconds, then zero on dispatch.
    Returns:
        Search receipt or SourceError with safe HTTP diagnostics. Same-process
        requests are serialized, with ten seconds after the previous provider
        attempt. This is local pacing, not a claimed provider-approved rate or
        guaranteed unblock. Cancellation remains responsive; no retry is made.
    """
    while not _search_lock.acquire(timeout=.25):
        cancelled()
    try:
        cancelled()
        wait=max(0, _search_finished.get(provider, float('-inf')) + SEARCH_INTERVAL_SECONDS - time.monotonic())
        if wait and on_wait:on_wait(wait)
        deadline=time.monotonic()+wait
        while time.monotonic()<deadline:
            cancelled();time.sleep(min(.1,max(0,deadline-time.monotonic())))
        cancelled()
        if on_wait:on_wait(0)
        try:
            return _search_once(query,limit,cancelled,provider)
        finally:
            _search_finished[provider]=time.monotonic()
    finally:
        _search_lock.release()


def _search_once(query, limit, cancelled, provider="duckduckgo"):
    """Execute one explicit free DDGS search in a credential-free subprocess.

    Args:
        query: Already-prepared query, unchanged.
        limit: Maximum returned URLs, normally five.
        cancelled: Callback checked while waiting for the isolated worker.
        provider: Explicit engine, duckduckgo or brave; no automatic fallback.
    Returns:
        Parsed search receipt, including normalized result references. Nonzero,
        timed-out or unrecognized/blocked replies raise SourceError, not empty success.
        This function never downloads result pages or substitutes another engine.
    """
    environment = {k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL','SYSTEMROOT')}
    process = subprocess.Popen([sys.executable,'-m','projectclarity.search.web_search','--search-worker'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,env=environment)
    payload=json.dumps({'query':query,'limit':limit,'provider':provider})
    started=time.monotonic()
    try:
        while True:
            cancelled()
            if time.monotonic()-started > 30:
                raise SourceError('search_timeout','Search exceeded 30 seconds.')
            try:
                output,_=process.communicate(payload,timeout=.25)
                break
            except subprocess.TimeoutExpired:
                payload=None
        if len(output)>100000:
            raise SourceError('search_error','Search response exceeded its output limit.')
        result=json.loads(output)
        if process.returncode:
            raise SourceError(result.get('code','search_error'),result.get('error','Search provider failed.'),result)
        return result
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def search_worker():
    """Call one maintained DDGS engine directly, avoiding automatic fallback/expansion.

    Args:
        None. Reads bounded JSON from stdin and writes one bounded JSON receipt.
    Returns:
        Exit code, with status/result or failure data on stdout. One provider page
        only; blocks are not bypassed and empty unrecognized pages are failures.
    """
    from ddgs.engines import ENGINES
    observed=[]
    request={}
    try:
        request=json.loads(sys.stdin.read(10000))
        provider=request.get('provider','duckduckgo')
        if provider not in ('duckduckgo','brave'):
            raise ValueError('Unsupported explicit search provider')
        base=ENGINES['text'][provider]

        class Observed(base):
            """Engine wrapper retaining actual HTTP status rather than swallowing blocks."""
            def request(self, *args, **kwargs):
                """Inspect the same single search response before the library parses it.

                Args:
                    args: Engine HTTP method and URL.
                    kwargs: Engine request parameters.
                Returns:
                    HTML for a successful response; failures raise without retries.
                """
                response=self.http_client.request(*args,**kwargs)
                markers=[m for m in ('captcha-container','unusual traffic from your computer network','challenge-form','anomaly-modal','bots use duckduckgo') if m in response.text.lower()]
                observed.append({'status':response.status_code,'challenge_markers':markers,
                                 'response_bytes':len(response.content),'body_sha256':hashlib.sha256(response.content).hexdigest()})
                if response.status_code!=200 or markers:
                    raise ValueError('Search provider returned '+('an anti-bot challenge' if markers else 'an unavailable response')+' (HTTP '+str(response.status_code)+'). No results accepted or challenge bypassed.')
                return response.text

        engine=Observed(timeout=15)
        rows=engine.search(request['query'],region='ca-en')
        if not rows:
            raise ValueError('Search returned no recognizable results; not treated as confirmed zero results.')
        results=[{'url':r.href[:2048],'title':r.title[:500],'snippet':r.body[:1000]} for r in rows[:request['limit']]]
        print(json.dumps({'provider':request.get('provider','duckduckgo'),'http':observed,'results':results}))
        return 0
    except Exception as error:
        blocked=any(h['status'] in (202,403,429) or h['challenge_markers'] for h in observed)
        print(json.dumps({'provider':request.get('provider','duckduckgo'),'http':observed,'code':'search_blocked' if blocked else 'search_error','error':str(error)[:1000]}))
        return 1


if __name__ == '__main__':
    raise SystemExit(search_worker())
