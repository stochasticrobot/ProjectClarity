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
from tempfile import TemporaryDirectory
from pathlib import Path
from functools import partial
from threading import Lock
from urllib.parse import urlencode, quote
from bs4 import BeautifulSoup
from projectclarity.search.public_search import search_public
from projectclarity.documents.web_acquisition import SourceError, http_get

SEARCH_INTERVAL_SECONDS = 10
_search_lock = Lock()
_search_finished = {}

def search_wikipedia(query, limit, cancelled, transport=http_get):
    """Search English Wikipedia once through its documented public search API.

    Args:
        query: Saved primary search query, unchanged; no model-generated broadening.
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


def search_web(query, limit, cancelled, provider="duckduckgo", on_wait=None, on_challenge=None):
    """Pace same-provider searches and preserve observed failure diagnostics.

    Args:
        query: Saved search query, unchanged.
        limit: Requested result count.
        cancelled: Callback that raises when scheduling should stop.
        provider: duckduckgo, brave, mwmbl, bing_rss or wikipedia; no automatic fallback.
        on_wait: Optional callback receiving cooldown seconds, then zero on dispatch.
        on_challenge: Optional human handoff callback; keeps the isolated DuckDuckGo
            client alive until the user submits, skips or cancels. No automatic solving.
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
            if provider in ('mwmbl', 'bing_rss'):
                return search_public(query,limit,cancelled,provider)
            if provider == 'wikipedia':
                return search_wikipedia(query,limit,cancelled)
            if on_challenge is not None and provider == "duckduckgo":
                return _search_once(query,limit,cancelled,provider,on_challenge)
            return _search_once(query,limit,cancelled,provider)
        finally:
            _search_finished[provider]=time.monotonic()
    finally:
        _search_lock.release()


def _search_once(query, limit, cancelled, provider="duckduckgo", on_challenge=None):
    """Run a credential-free worker, retaining its client during human verification.

    Args:
        query: Prepared query, unchanged; limit: Maximum returned leads.
        cancelled: Cooperative cancellation callback that terminates the worker.
        provider: Explicit DuckDuckGo or Brave engine.
        on_challenge: Optional callback returning the user's submit/skip/cancel
            decision from safe images/instructions. None retains noninteractive behavior.
    Returns:
        Search receipt or sanitized SourceError. Human waits suspend the network
        deadline, not cancellation. Private temporary handoff files are removed on
        every exit; cookies and provider form tokens never leave the worker.
    """
    environment = {k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL','SYSTEMROOT')}
    with TemporaryDirectory(prefix='clarity-captcha-') as directory:
        root = Path(directory)
        process = subprocess.Popen([sys.executable,'-m','projectclarity.search.web_search','--search-worker'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,env=environment)
        request = {'query':query,'limit':limit,'provider':provider}
        if on_challenge is not None and provider == 'duckduckgo':
            request['captcha_directory'] = directory
        payload = json.dumps(request)
        started, revision = time.monotonic(), None
        try:
            while True:
                cancelled()
                challenge_file = root / 'challenge.json'
                if on_challenge is not None and challenge_file.exists():
                    if challenge_file.stat().st_size > 24000000:
                        raise SourceError('search_error', 'Challenge payload too large.')
                    challenge = json.loads(challenge_file.read_text())
                    if challenge['revision'] != revision:
                        answer = on_challenge(challenge)
                        cancelled()
                        revision = challenge['revision']
                        temporary = root / 'answer.tmp'
                        temporary.write_text(json.dumps({'revision':revision, **answer}))
                        temporary.chmod(0o600); temporary.replace(root / 'answer.json')
                        started = time.monotonic()
                if time.monotonic()-started > (120 if on_challenge is not None else 30):
                    raise SourceError('search_timeout','Search exceeded its active-request time limit.')
                try:
                    output,_ = process.communicate(payload,timeout=.25)
                    break
                except subprocess.TimeoutExpired:
                    payload = None
            if len(output)>100000:
                raise SourceError('search_error','Search response exceeded its output limit.')
            result = json.loads(output)
            if process.returncode:
                if result.get('code') == 'search_cancelled':
                    raise InterruptedError('Research cancelled during human verification.')
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
        Exit code, with status/result or failure data on stdout. An interactive
        DuckDuckGo request retains its client while private file IPC waits for a
        human answer; other requests retain single-provider, noninteractive behavior.
    """
    from ddgs.engines import ENGINES
    observed=[]
    request={}
    try:
        request=json.loads(sys.stdin.read(10000))
        provider=request.get('provider','duckduckgo')
        if provider == 'duckduckgo' and request.get('captcha_directory'):
            from projectclarity.search.duckduckgo_session import search_session, exchange_challenge
            try:
                result = search_session(request['query'],request['limit'],partial(exchange_challenge,request['captcha_directory']))
                print(json.dumps(result)); return 0
            except InterruptedError:
                print(json.dumps({'code':'search_cancelled','error':'Human verification cancelled.'})); return 1
            except SourceError as error:
                print(json.dumps({'code':error.code,'error':str(error),'diagnostic':error.details})); return 1
            except Exception:
                print(json.dumps({'code':'search_error','error':'DuckDuckGo verification request failed.'})); return 1
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
