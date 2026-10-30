"""Reject known advertising endpoints and unwrap known organic search redirects locally.

This is a deliberately narrow URL-shape filter, not an ad-content classifier.
Unknown websites and ordinary query parameters are preserved. No redirect is
requested over the network; destination policy and retrieval checks still apply.
"""
import base64
import binascii
from urllib.parse import parse_qs, unquote, urlsplit

from projectclarity.discovery.observed_link_inventory import canonical_url


def normalize_search_result_url(value):
    """Recover known organic destinations without following advertising click links.

    Args:
        value: Raw provider URL, or a supplied URL being considered for the probe.
    Returns:
        A pair of canonical URL and optional exclusion code. Surrounding Unicode
        whitespace is trimmed from each decoded destination; embedded whitespace
        remains invalid. Known advertising
        endpoints retain their URL for audit but are excluded. Malformed, private,
        cyclic or over-nested organic wrappers are excluded without any HTTP/DNS
        access. At most three wrappers are decoded; unknown URL shapes are unchanged.
        The caller must still enforce its domain policy and normal fetch safeguards.
    """
    seen = set()
    for depth in range(4):
        url = canonical_url(value)
        if not url:
            return None, 'invalid_or_nonpublic_url'
        if url in seen:
            return url, 'invalid_search_redirect'
        seen.add(url)
        parts = urlsplit(url)
        host, path = parts.hostname, parts.path.rstrip('/')
        if ((host in {'bing.com', 'www.bing.com'} and path == '/aclick')
                or host in {'googleadservices.com', 'www.googleadservices.com', 'ad.doubleclick.net'}
                or (host in {'google.com', 'www.google.com', 'www.google.ca'}
                    and path in {'/aclk', '/pagead/aclk'})
                or (host == 'bat.bing.com' and path.startswith('/action/'))):
            return url, 'advertising_click_url'
        params = parse_qs(parts.query)
        target, wrapper = None, False
        if host in {'duckduckgo.com', 'www.duckduckgo.com', 'links.duckduckgo.com'} and path == '/l':
            wrapper = True
            target = params.get('uddg', [None])[0]
        elif host in {'google.com', 'www.google.com', 'www.google.ca'} and path == '/url':
            wrapper = True
            target = params.get('url', params.get('q', [None]))[0]
        elif host == 'r.search.yahoo.com' and '/RU=' in parts.path:
            wrapper = True
            target = unquote(parts.path.split('/RU=', 1)[1].split('/RK=', 1)[0].split('/RS=', 1)[0])
        elif host in {'bing.com', 'www.bing.com'} and path == '/ck/a':
            wrapper = True
            encoded = params.get('u', [''])[0]
            if encoded.startswith('a1'):
                encoded = encoded[2:]
                try:
                    target = base64.b64decode(encoded + '=' * (-len(encoded) % 4),
                        altchars=b'-_', validate=True).decode('utf-8')
                except (ValueError, binascii.Error, UnicodeError):
                    pass
        if not wrapper:
            return url, None
        if not target or depth == 3:
            return url, 'invalid_search_redirect'
        value = target
    return None, 'invalid_search_redirect'
