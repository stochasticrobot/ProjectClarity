"""Retain one isolated DuckDuckGo client while a human answers its image challenge.

The parent application receives cached image tiles, never raw HTML, form tokens or
cookies. The same client submits the human's answer and reads the result page. This
module does not identify images, solve challenges, or run the research pipeline.
"""
import base64
import json
from pathlib import Path
import time
from urllib.parse import urljoin, urlsplit, parse_qs, urlencode
from uuid import uuid4
from bs4 import BeautifulSoup
from projectclarity.documents.web_acquisition import SourceError, canonical_url

BASE = 'https://html.duckduckgo.com/html/'


def provider_url(url):
    """Reject form, image or redirect targets outside the expected provider hosts.

    Args: url: Absolute resolved target from the provider response.
    Returns: Validated HTTPS URL, or SourceError without exposing the target tokens.
    """
    p = urlsplit(url)
    if p.scheme != 'https' or p.hostname not in ('duckduckgo.com', 'html.duckduckgo.com') or p.username or p.password or p.port not in (None, 443):
        raise SourceError('search_error', 'Unexpected DuckDuckGo challenge target.')
    return url


def provider_request(client, method, url, data=None):
    """Read a bounded response through the same client, following only allowed redirects.

    Args: client: Retained HTTP client; method: GET/POST; url: Provider target;
        data: Optional form fields, including user-selected answers when submitting.
    Returns: Response and final URL. Rejects excessive redirects or oversized replies;
        never logs form fields, cookies or raw provider URLs.
    """
    for _ in range(5):
        provider_url(url)
        options = {'follow_redirects': False}
        if data is not None:
            options['data'] = data
        response = client.request(method, url, **options)
        if len(response.content) > 2000000:
            raise SourceError('search_error', 'DuckDuckGo response exceeded its size limit.')
        if response.status_code not in (301, 302, 303, 307, 308):
            return response, url
        location = response.headers.get('location') or response.headers.get('Location')
        if not location:
            raise SourceError('search_error', 'DuckDuckGo redirect has no destination.')
        url = provider_url(urljoin(url, location))
        if response.status_code in (301, 302, 303):
            method, data = 'GET', None
    raise SourceError('search_error', 'DuckDuckGo returned too many redirects.')


def exchange_challenge(directory, challenge):
    """Send safe challenge data to the parent and await its explicit human decision.

    Args: directory: Private temporary IPC directory owned by the parent;
        challenge: Instruction and base64 raster tiles, excluding provider tokens.
    Returns: Matching submit/skip/cancel response. Parent cancellation kills this
        isolated worker; old answers cannot match a renewed challenge revision.
    """
    root = Path(directory)
    revision = str(uuid4())
    packet = {'revision': revision, **challenge}
    temp = root / 'challenge.tmp'
    temp.write_text(json.dumps(packet)); temp.chmod(0o600)
    temp.replace(root / 'challenge.json')
    while True:
        path = root / 'answer.json'
        if path.exists():
            answer = json.loads(path.read_text())
            if answer.get('revision') == revision:
                return answer
        time.sleep(.1)


def search_session(query, limit, ask, client=None):
    """Run one query with a retained client and human-only challenge continuation.

    Args:
        query: Original prepared query, unchanged during the handoff.
        limit: Maximum accepted result leads.
        ask: Callback receiving safe images/instructions and returning a human decision.
        client: Optional controlled HTTP client for tests; otherwise creates DDGS's client.
    Returns:
        Search receipt only after actual result links are parsed. Empty results stay
        empty so the caller can fall back. Skip raises SourceError; cancel raises
        InterruptedError. Renewed puzzles get a fresh handoff (maximum five rounds).
        All cookies/tokens stay inside this worker. No automatic answers or retries.
    """
    if client is None:
        from ddgs.engines.duckduckgo import Duckduckgo
        client = Duckduckgo(timeout=20).http_client.client
    response, url = provider_request(client, 'POST', BASE, {'q': query, 'b': '', 'l': 'ca-en'})
    solved = 0
    for _ in range(12):
        soup = BeautifulSoup(response.text, 'html.parser')
        form = soup.select_one('form#challenge-form')
        if form:
            if solved >= 5:
                raise SourceError('search_blocked', 'DuckDuckGo kept issuing challenges; trying the next provider.')
            action = provider_url(urljoin(url, form['action']))
            fields = {e['name']: e.get('value', '') for e in form.select('input[type="hidden"][name]')}
            submit = form.select_one('button[name="challenge-submit"]')
            boxes = form.select('.anomaly-modal__box')
            if submit is None or not 1 <= len(boxes) <= 16:
                raise SourceError('search_blocked', 'Unsupported DuckDuckGo challenge layout.')
            fields[submit['name']] = submit.get('value', '')
            images, names = [], []
            for box in boxes:
                checkbox, image = box.select_one('input[type="checkbox"][name]'), box.select_one('img[src]')
                if checkbox is None or image is None:
                    raise SourceError('search_blocked', 'Unsupported DuckDuckGo challenge tile.')
                names.append(checkbox['name'])
                picture, _ = provider_request(client, 'GET', provider_url(urljoin(url, image['src'])))
                mime = picture.headers.get('content-type', '').split(';')[0]
                if picture.status_code != 200 or mime not in ('image/jpeg', 'image/png', 'image/webp') or len(picture.content) > 1000000:
                    raise SourceError('search_blocked', 'DuckDuckGo challenge image is unavailable.')
                images.append({'mime': mime, 'data': base64.b64encode(picture.content).decode()})
            instruction = form.select_one('.anomaly-modal__instructions')
            answer = ask({'instruction': instruction.get_text(' ', strip=True) if instruction else 'Complete the image challenge.', 'images': images})
            if answer['action'] == 'cancel':
                raise InterruptedError('Research cancelled during human verification.')
            if answer['action'] == 'skip':
                raise SourceError('search_skipped', 'Human verification skipped; trying the next provider.')
            if answer['action'] != 'submit' or any(type(i) is not int or not 0 <= i < len(names) for i in answer['tiles']):
                raise SourceError('search_error', 'Invalid human verification response.')
            for i in answer['tiles']:
                fields[names[i]] = 'on'
            response, url = provider_request(client, 'POST', action, fields)
            solved += 1
            continue
        if response.status_code != 200:
            raise SourceError('search_error', f'DuckDuckGo returned HTTP {response.status_code}.', {'http_status': response.status_code})
        success = soup.select_one('form#success-form[action]')
        if success:
            target = provider_url(urljoin(url, success['action']))
            method = success.get('method', 'get').upper()
            if method not in ('GET', 'POST'):
                raise SourceError('search_error', 'Unexpected continuation method.')
            fields = {e['name']: e.get('value', '') for e in success.select('input[name]')}
            if method == 'GET':
                if fields: target += ('&' if '?' in target else '?') + urlencode(fields)
                fields = None
            response, url = provider_request(client, method, target, fields)
            continue
        results = []
        for link in soup.select('a.result__a[href]'):
            target = urljoin(url, link['href'])
            target = parse_qs(urlsplit(target).query).get('uddg', [target])[0]
            try:
                target = canonical_url(target)
            except ValueError:
                continue
            parent = link.find_parent(class_='result')
            snippet = parent.select_one('.result__snippet') if parent else None
            results.append({'url': target, 'title': link.get_text(' ', strip=True)[:500],
                            'snippet': snippet.get_text(' ', strip=True)[:1000] if snippet else ''})
            if len(results) >= limit: break
        return {'provider': 'duckduckgo', 'http_status': response.status_code,
                'human_submissions': solved, 'results': results}
    raise SourceError('search_error', 'DuckDuckGo continuation limit reached.')
