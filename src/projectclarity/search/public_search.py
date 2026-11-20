"""Read account-free Mwmbl and Bing RSS search results as document leads.

These adapters make one bounded request with the original query and return titles,
URLs and snippets. They do not download result pages, generate answers, retry failed
requests or decide fallback order. The shared search runner owns pacing and order.
"""
import json
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from projectclarity.documents.web_acquisition import SourceError, http_get


def search_public(query, limit, cancelled, provider, transport=http_get):
    """Fetch and validate one Mwmbl JSON or Bing RSS response without credentials.

    Args:
        query: Prepared search keywords, passed unchanged to the provider.
        limit: Maximum number of result leads returned.
        cancelled: Callback raising InterruptedError when work must stop.
        provider: Either mwmbl or bing_rss; other names are rejected.
        transport: Injectable single-hop HTTP reader enforcing byte/time limits.
    Returns:
        Provider receipt with HTTP status, query and normalized leads, possibly
        empty. HTTP, provider-reported, malformed and unsafe XML replies raise
        SourceError with bounded diagnostics. No retry or redirect is attempted.
    """
    if provider == 'mwmbl':
        url = 'https://api.mwmbl.org/api/v2/search/?' + urlencode({'q': query})
    elif provider == 'bing_rss':
        url = 'https://www.bing.com/search?' + urlencode({'q': query, 'format': 'rss'})
    else:
        raise ValueError('Unsupported public search provider.')
    cancelled()
    status, _, body = transport(url, 1000000, cancelled)
    details = {'provider': provider, 'http_status': status}
    if status != 200:
        raise SourceError('search_error', f'{provider} search returned HTTP {status}.', details)
    try:
        if provider == 'mwmbl':
            data = json.loads(body)
            if not isinstance(data, dict) or data.get('error') or data.get('errors') or data.get('detail') or data.get('success') is False:
                raise ValueError('Provider reported an error or returned an invalid envelope.')
            rows = data['results']
            if not isinstance(rows, list):
                raise ValueError('Invalid result list.')
            results = [{'url': row['url'], 'title': row['title'],
                        'snippet': row.get('content', '')} for row in rows[:limit]]
        else:
            # RSS needs neither DTDs nor entities; reject them before XML parsing.
            if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
                raise ValueError('Unsupported RSS declarations.')
            root = ET.fromstring(body)
            channel = root.find('channel')
            if root.tag != 'rss' or channel is None or root.find('.//error') is not None:
                raise ValueError('Provider returned an error or non-RSS response.')
            results = [{'url': row.findtext('link'), 'title': row.findtext('title'),
                        'snippet': row.findtext('description') or ''}
                       for row in channel.findall('item')[:limit]]
        if any(not isinstance(row[key], str) for row in results for key in ('url', 'title', 'snippet')):
            raise ValueError('Invalid result fields.')
    except (ValueError, TypeError, KeyError, ET.ParseError) as error:
        raise SourceError('search_error', f'{provider} returned an invalid or error search response.', details) from error
    return {**details, 'query': query, 'results': results}
