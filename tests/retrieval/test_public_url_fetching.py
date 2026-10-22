"""Check the boundaries around website access with no real network traffic.

For example, a public page redirecting to localhost must be rejected before a
connection to localhost is attempted. These tests keep our HTTP policy code real
and replace the connection pool/DNS results with fixtures. They also inspect the
IP, TLS hostname, headers, retries, and actual bytes read from each fake response.
"""

import socket
import unittest
from unittest.mock import patch

from projectclarity.retrieval.retrieval_limits import RetrievalBudget, RetrievalError, RetrievalLimits
from projectclarity.retrieval.public_url_fetcher import PublicWebReader, public_target
from tests.retrieval.retrieval_test_fixtures import FakeHTTP, public_dns


class WebTests(unittest.TestCase):
    """No website or live DNS is needed to falsify the access-control assumptions."""

    def setUp(self):
        """Replace DNS and HTTP pools and deny sockets, keeping URL/robots/budget code real."""
        self.budget = RetrievalBudget(RetrievalLimits(min_host_interval_seconds=0.0))
        self.reader = PublicWebReader(self.budget)
        self.routes = {("example.org", "/robots.txt"): (200, {"Content-Type": "text/plain"}, b"User-agent: *\nAllow: /\n"),
                       ("example.org", "/page"): (200, {"Content-Type": "text/plain"}, b"Source text")}
        self.http = FakeHTTP(self.routes)
        for target, arguments in [
            ("socket.getaddrinfo", {"side_effect": public_dns}),
            ("socket.socket.connect", {"side_effect": AssertionError("No sockets allowed")}),
            ("projectclarity.retrieval.public_url_fetcher.urllib3.HTTPSConnectionPool", {"side_effect": self.http.pool}),
            ("projectclarity.retrieval.public_url_fetcher.urllib3.HTTPConnectionPool", {"side_effect": self.http.pool}),
        ]:
            mock = patch(target, **arguments)
            mock.start()
            self.addCleanup(mock.stop)

    def test_public_fetch_pins_ip_preserves_tls_identity_and_bounds_requests(self):
        """An allowed page uses the checked IP but verifies the website's certificate name."""
        response, final_url, chain = self.reader.fetch("https://example.org/page")
        self.assertEqual(response.body, b"Source text")
        self.assertEqual(final_url, "https://example.org/page")
        self.assertEqual(chain, [final_url])
        self.assertEqual(self.budget.http_requests, 2)  # Robots is a real budgeted request.
        for ip, options in self.http.connections:
            self.assertEqual(ip, "93.184.216.34")
            self.assertEqual(options["server_hostname"], "example.org")
            self.assertEqual(options["assert_hostname"], "example.org")
            self.assertEqual(options["cert_reqs"], "CERT_REQUIRED")
        for _, _, options in self.http.requests:
            self.assertIs(options["redirect"], False)
            self.assertIs(options["retries"], False)
            self.assertEqual(options["headers"]["Host"], "example.org")
            self.assertNotIn("Authorization", options["headers"])

    def test_private_loopback_metadata_and_non_http_targets_are_blocked(self):
        """Private addresses cannot be reached directly or through unusual URL spellings."""
        urls = ["http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/",
                "http://localhost/", "http://[::1]/", "http://[::ffff:8.8.8.8]/", "file:///etc/passwd",
                "https://example.org:8443/", "https://user:password@example.org/", "https://example.org/\nsecret"]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(RetrievalError):
                    self.reader.fetch(url)
        self.assertEqual(self.http.requests, [])

    def test_mixed_public_and_private_dns_answers_are_refused(self):
        """Choosing only the public answer would hide an unsafe DNS configuration."""
        answers = public_dns("example.org", 443) + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
        with patch("socket.getaddrinfo", return_value=answers):
            with self.assertRaises(RetrievalError) as caught:
                public_target("https://example.org/page")
        self.assertEqual(caught.exception.code, "blocked_address")
        self.assertEqual(self.http.requests, [])

    def test_robots_denial_and_unavailable_rules_do_not_fetch_page(self):
        """Wildcard exclusions and robots failures stop acquisition before page access."""
        for status, body in [(200, b"User-agent: *\nDisallow: /pa*\n"), (403, b""), (503, b"")]:
            with self.subTest(status=status):
                self.routes[("example.org", "/robots.txt")] = (status, {"Content-Type": "text/plain"}, body)
                reader = PublicWebReader(self.budget)
                with self.assertRaises(RetrievalError):
                    reader.fetch("https://example.org/page")
        self.assertTrue(all(path == "/robots.txt" for _, path, _ in self.http.requests))

    def test_missing_robots_allows_page_and_rules_are_cached(self):
        """A missing rules file is allowed; the same origin need not refetch it for every source."""
        self.routes[("example.org", "/robots.txt")] = (404, {}, b"")
        self.reader.fetch("https://example.org/page")
        self.reader.fetch("https://example.org/page")
        self.assertEqual([path for _, path, _ in self.http.requests], ["/robots.txt", "/page", "/page"])

    def test_redirect_is_rechecked_and_private_target_is_never_requested(self):
        """An allowed starting address cannot smuggle an internal target through a redirect."""
        self.routes[("example.org", "/page")] = (302, {"Location": "https://127.0.0.1/secret"}, b"")
        with self.assertRaises(RetrievalError) as caught:
            self.reader.fetch("https://example.org/page")
        self.assertEqual(caught.exception.code, "blocked_address")
        self.assertEqual(len(self.http.requests), 2)

    def test_public_redirect_chain_is_retained_and_loop_is_rejected(self):
        """Provenance keeps redirect hops; a loop does not spend unlimited requests."""
        self.routes[("example.org", "/page")] = (302, {"Location": "/final"}, b"")
        self.routes[("example.org", "/final")] = (200, {"Content-Type": "text/plain"}, b"Final text")
        _, final_url, chain = self.reader.fetch("https://example.org/page")
        self.assertEqual(chain, ["https://example.org/page", "https://example.org/final"])
        self.assertEqual(final_url, chain[-1])
        self.routes[("example.org", "/final")] = (302, {"Location": "/page"}, b"")
        with self.assertRaises(RetrievalError) as caught:
            self.reader.fetch("https://example.org/page")
        self.assertEqual(caught.exception.code, "redirect_loop")

    def test_large_stream_and_compressed_response_are_not_accepted(self):
        """Actual byte counting protects against missing lengths; compression is explicit unsupported input."""
        self.budget.limits = RetrievalLimits(max_source_bytes=4, min_host_interval_seconds=0.0)
        for headers, expected in [({"Content-Type": "text/plain"}, "source_size_limit"),
                                  ({"Content-Encoding": "gzip"}, "encoded_response")]:
            with self.subTest(expected=expected):
                self.routes[("example.org", "/page")] = (200, headers, b"Too many bytes")
                with self.assertRaises(RetrievalError) as caught:
                    self.reader.fetch("https://example.org/page")
                self.assertEqual(caught.exception.code, expected)

    def test_request_and_total_byte_budgets_stop_work(self):
        """Robots traffic counts against both limits, not just successful source pages."""
        for limits, expected in [(RetrievalLimits(max_http_requests=1, min_host_interval_seconds=0.0), "request_limit"),
                                 (RetrievalLimits(max_total_bytes=4, min_host_interval_seconds=0.0), "total_size_limit")]:
            with self.subTest(expected=expected):
                with self.assertRaises(RetrievalError) as caught:
                    PublicWebReader(RetrievalBudget(limits)).fetch("https://example.org/page")
                self.assertEqual(caught.exception.code, expected)

    def test_crawl_delay_and_rate_limit_are_respected(self):
        """Honor the site's delay; after HTTP 429, do not repeatedly hit the origin."""
        self.routes[("example.org", "/robots.txt")] = (200, {}, b"User-agent: *\nCrawl-delay: 2\n")
        self.routes[("example.org", "/page")] = (429, {}, b"")
        with patch("projectclarity.retrieval.public_url_fetcher.time.sleep") as sleep:
            with self.assertRaises(RetrievalError):
                self.reader.fetch("https://example.org/page")
            self.assertTrue(sleep.called)
            # Pacing is now split into short cancellable waits. Their combined
            # duration still honors the site's requested delay.
            self.assertGreater(sum(call.args[0] for call in sleep.call_args_list), 1)
            self.assertTrue(all(0 < call.args[0] <= 0.25 for call in sleep.call_args_list))
        before = len(self.http.requests)
        with self.assertRaises(RetrievalError):
            self.reader.fetch("https://example.org/page")
        self.assertEqual(len(self.http.requests), before)
