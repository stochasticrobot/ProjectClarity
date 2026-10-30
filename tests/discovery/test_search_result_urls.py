"""Check local cleanup of search-result URLs before source selection.

Fixed URL strings cover surrounding whitespace, known organic redirects, advertising
links, excessive nesting and unsafe destinations. The real normalizer runs without
following links. Tests preserve legitimate paths and query parameters rather than
using broad keyword filters to hide difficult cases.
"""
import base64
import unittest
from urllib.parse import quote

from projectclarity.discovery.search_result_urls import normalize_search_result_url


class SearchResultURLTests(unittest.TestCase):
    """Exercise offline URL transformations; these tests never contact providers."""

    def test_surrounding_unicode_whitespace_does_not_discard_a_destination(self):
        """Preserve the actual Commons directory result despite provider formatting noise.

        Args:
            self: Offline case covering plain URLs and decoded organic destinations.
        Returns:
            None. Assert boundary whitespace is removed, fragments still deduplicate,
            and encoded spaces inside paths and queries retain their original meaning.
        """
        target = 'https://www.ourcommons.ca/Members'
        for padding in [' ', '\u00a0', '\u2003', '\t\r\n']:
            for raw in [target + padding, padding + target, padding + target + padding]:
                with self.subTest(raw=repr(raw)):
                    self.assertEqual(normalize_search_result_url(raw), (target, None))
                    self.assertEqual(normalize_search_result_url(
                        'https://www.google.com/url?q=' + quote(raw, safe='')), (target, None))
        self.assertEqual(normalize_search_result_url(' \u00a0' + target + '#top\u00a0'), (target, None))
        encoded = 'https://example.org/a%20b?q=hello%20world'
        self.assertEqual(normalize_search_result_url(' ' + encoded + ' '), (encoded, None))

    def test_trimming_does_not_repair_embedded_whitespace_or_unsafe_addresses(self):
        """Boundary cleanup cannot change the URL's internal structure or bypass restrictions.

        Args:
            self: Offline case with embedded spaces, control characters and unsafe URLs.
        Returns:
            None. Assert malformed/unsafe destinations remain excluded even inside
            a supported organic wrapper; no network requests are made.
        """
        for target in ['https://www.ourcommons.ca/Mem bers',
                'https://www.ourcommons.ca/Mem\u00a0bers', 'https://exam\tple.org/',
                'https://example.org/a\nb', 'https://example.org/?q=two words',
                'https://example.org/\x00', 'https://example.org/\u200b',
                'http://127.0.0.1/', 'http://host.internal/', 'file:///etc/passwd',
                'https://user:password@example.org/']:
            raw = '\u00a0' + target + ' '
            with self.subTest(target=repr(target)):
                self.assertEqual(normalize_search_result_url(raw), (None, 'invalid_or_nonpublic_url'))
                self.assertEqual(normalize_search_result_url(
                    'https://www.google.com/url?q=' + quote(raw, safe='')),
                    (None, 'invalid_or_nonpublic_url'))

    def test_known_advertising_endpoints_are_not_unwrapped(self):
        """A potentially useful destination does not justify following an ad click URL.

        Args:
            self: Case testing explicit host/path shapes, not ad-related keywords.
        Returns:
            None. Assert ad exclusions even when the wrapper contains a valid destination.
        """
        for url in ['https://www.bing.com/aclick?u=https://example.org/roster',
                'https://www.googleadservices.com/pagead/aclk?adurl=https://example.org',
                'https://www.google.com/aclk?adurl=https://example.org',
                'https://ad.doubleclick.net/ddm/clk/123']:
            with self.subTest(url=url):
                self.assertEqual(normalize_search_result_url(url), (url, 'advertising_click_url'))

    def test_known_organic_wrappers_resolve_without_network(self):
        """Preserve the real destination, including meaningful query parameters.

        Args:
            self: Case covering the supported Google, DDG, Bing and Yahoo wrappers.
        Returns:
            None. Assert identical canonical destinations from each supported encoding.
        """
        target = 'https://example.org/roster?year=2026&group=security'
        encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip('=')
        urls = ['https://www.google.com/url?q=' + quote(target, safe=''),
            'https://duckduckgo.com/l/?uddg=' + quote(target, safe=''),
            'https://www.bing.com/ck/a?u=a1' + encoded,
            'https://r.search.yahoo.com/opaque/RU=' + quote(target, safe='') + '/RK=2/RS=opaque']
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(normalize_search_result_url(url), (target, None))

    def test_private_credential_and_non_http_destinations_are_rejected(self):
        """Unwrapping cannot bypass public-URL shape restrictions.

        Args:
            self: Case providing unsafe destinations through an otherwise valid wrapper.
        Returns:
            None. Assert no usable destination for private, credentialed or non-HTTP URLs.
        """
        for target in ['http://127.0.0.1/', 'http://[::1]/', 'http://host.internal/',
                'https://user:password@example.org/', 'file:///etc/passwd', 'javascript:alert(1)']:
            with self.subTest(target=target):
                self.assertEqual(normalize_search_result_url(
                    'https://www.google.com/url?q=' + quote(target, safe='')),
                    (None, 'invalid_or_nonpublic_url'))

    def test_wrapped_advertisements_are_still_excluded(self):
        """An organic tracking layer cannot launder a known advertising endpoint.

        Args:
            self: Case nesting the observed Bing ad shape in a Yahoo organic wrapper.
        Returns:
            None. Assert the final exclusion remains advertising_click_url.
        """
        target = 'https://www.bing.com/aclick?ld=opaque'
        url = 'https://r.search.yahoo.com/RU=' + quote(target, safe='') + '/RK=2/RS=opaque'
        self.assertEqual(normalize_search_result_url(url), (target, 'advertising_click_url'))

    def test_unknown_sites_and_legitimate_paths_are_unchanged(self):
        """Avoid broad domain/keyword filters that would hide useful research pages.

        Args:
            self: Case including legitimate ad-research pages and lookalike domain names.
        Returns:
            None. Assert ordinary URL semantics are preserved for later policy/fetch checks.
        """
        for url in ['https://example.org/research/advertising?url=meaningful',
                'https://www.bing.com/research/', 'https://google.com.example.org/url?q=unchanged',
                'https://example.org/redirect?target=https://another.example.org/']:
            with self.subTest(url=url):
                self.assertEqual(normalize_search_result_url(url), (url, None))

    def test_malformed_and_over_nested_wrappers_are_excluded(self):
        """Bound local decoding and do not fetch a wrapper whose destination is unknown.

        Args:
            self: Case with invalid/missing encoding and excessive organic nesting.
        Returns:
            None. Assert explicit invalid_search_redirect without exceptions or network use.
        """
        nested = 'https://example.org/'
        for _ in range(4):
            nested = 'https://www.google.com/url?q=' + quote(nested, safe='')
        for url in ['https://www.bing.com/ck/a?u=a1%%%%',
                'https://www.bing.com/ck/a?u=unknown_encoding',
                'https://www.google.com/url', nested]:
            with self.subTest(url=url):
                self.assertEqual(normalize_search_result_url(url)[1], 'invalid_search_redirect')
