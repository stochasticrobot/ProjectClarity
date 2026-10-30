"""Find candidate Wikipedia articles through its public full-text search API.

The isolated search worker calls this adapter when Wikipedia is the explicitly
selected engine. It sends a bounded query, decodes result titles and snippets,
and returns article URLs for later source selection. It does not download article
bodies. An empty Wikipedia result is not evidence that the topic is absent from
the wider web; failures remain distinct from successful empty responses.
"""
import json
from urllib.parse import quote

import certifi
import urllib3
from bs4 import BeautifulSoup

from projectclarity.discovery.search_provider import SearchProviderError
from projectclarity.discovery.search_transport import classify_response


def search_wikipedia(query, limit):
    """Query English Wikipedia's public full-text API within the worker deadline.

    Args:
        query: Unmodified research query; no invented topic-specific replacement.
        limit: Validated maximum number of article leads, between one and ten.

    Returns:
        Normalized URL/title/snippet dictionaries, or an explicit empty list.
        Raise SearchProviderError for HTTP blocks, API errors or malformed replies.
        Uses one fixed HTTPS endpoint, verified TLS, no redirects, no retries,
        at most 1 MB of response data, and no article-body requests. Snippets are
        unverified leads subject to normal source selection and acquisition.
    """
    with urllib3.PoolManager(cert_reqs='CERT_REQUIRED', ca_certs=certifi.where(),
            timeout=urllib3.Timeout(connect=5, read=8)) as http:
        response = http.request('GET', 'https://en.wikipedia.org/w/api.php',
            fields={'action': 'query', 'list': 'search', 'srsearch': query, 'srlimit': limit,
                'srnamespace': 0, 'srprop': 'snippet', 'format': 'json'},
            headers={'User-Agent': 'ProjectClarity/0.1 (research discovery; Python urllib3)'},
            retries=False, redirect=False, preload_content=False)
        try:
            body = response.read(1_000_001)
            if len(body) > 1_000_000:
                raise SearchProviderError('search_response_unrecognized')
            text = body.decode('utf-8')
            observed = classify_response(response.status, text, response.headers)
            if observed['code']:
                raise SearchProviderError(observed['code'], {k: v for k, v in observed.items() if k != 'code'})
            data = json.loads(text)
            if 'error' in data:
                raise SearchProviderError('search_service_unavailable', {'type': 'MediaWikiAPIError',
                    'message': str(data['error'].get('code', 'unknown'))[:100]})
            rows = data['query']['search']
            if not isinstance(rows, list):
                raise ValueError('Invalid result list')
            return [{'url': 'https://en.wikipedia.org/wiki/' + quote(row['title'].replace(' ', '_'), safe=''),
                'title': row['title'][:300],
                'snippet': BeautifulSoup(row.get('snippet', ''), 'html.parser').get_text(' ', strip=True)[:700]}
                for row in rows[:limit]]
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise SearchProviderError('search_response_unrecognized', {'type': type(exc).__name__}) from None
        finally:
            response.close()
