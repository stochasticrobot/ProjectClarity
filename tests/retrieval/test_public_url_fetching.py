"""Check the boundaries around website access with no real network traffic.

For example, a public page redirecting to localhost must be rejected before a
connection to localhost is attempted. These tests keep our HTTP policy code real
and replace the connection pool/DNS results with fixtures. They also inspect the
IP, TLS hostname, headers, retries, and actual bytes read from each fake response.
"""

import socket
import time
import unittest
from unittest.mock import patch

from projectclarity.retrieval.retrieval_limits import RetrievalBudget, RetrievalError, RetrievalLimits
from projectclarity.retrieval.public_url_fetcher import PublicWebReader, public_target
from tests.retrieval.retrieval_test_fixtures import FakeHTTP, public_dns


class WebTests(unittest.TestCase):
    """No website or live DNS is needed to falsify the access-control assumptions."""

    def setUp(self):
        """Replace DNS and HTTP pools and deny sockets, keeping URL, pacing and budget code real.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Initialize this test instance's fixtures; unittest calls setUp before each
            test method.
        """
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
        """An allowed page uses the checked IP but verifies the website's certificate name.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        response, final_url, chain = self.reader.fetch("https://example.org/page")
        self.assertEqual(response.body, b"Source text")
        self.assertEqual(final_url, "https://example.org/page")
        self.assertEqual(chain, [final_url])
        self.assertEqual(self.budget.http_requests, 1)  # Only the requested page is fetched.
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
        """Private addresses cannot be reached directly or through unusual URL spellings.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        urls = ["http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/",
                "http://localhost/", "http://[::1]/", "http://[::ffff:8.8.8.8]/", "file:///etc/passwd",
                "https://example.org:8443/", "https://user:password@example.org/", "https://example.org/\nsecret"]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(RetrievalError):
                    self.reader.fetch(url)
        self.assertEqual(self.http.requests, [])

    def test_mixed_public_and_private_dns_answers_are_refused(self):
        """Choosing only the public answer would hide an unsafe DNS configuration.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        answers = public_dns("example.org", 443) + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
        with patch("socket.getaddrinfo", return_value=answers):
            with self.assertRaises(RetrievalError) as caught:
                public_target("https://example.org/page")
        self.assertEqual(caught.exception.code, "blocked_address")
        self.assertEqual(self.http.requests, [])

    def test_robots_rules_and_failures_are_never_requested(self):
        """Robots deny rules and endpoint failures cannot affect requested page access.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        for status, body in [(200, b"User-agent: *\nDisallow: /pa*\n"), (403, b""), (503, b"")]:
            with self.subTest(status=status):
                self.routes[("example.org", "/robots.txt")] = (status, {"Content-Type": "text/plain"}, body)
                reader = PublicWebReader(self.budget)
                reply, _, _ = reader.fetch("https://example.org/page")
                self.assertEqual(reply.body, b"Source text")
        self.assertEqual([path for _, path, _ in self.http.requests], ["/page"] * 3)

    def test_repeated_fetches_do_not_probe_a_missing_robots_endpoint(self):
        """Repeated page requests never introduce a robots-policy lookup.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        self.routes[("example.org", "/robots.txt")] = (404, {}, b"")
        self.reader.fetch("https://example.org/page")
        self.reader.fetch("https://example.org/page")
        self.assertEqual([path for _, path, _ in self.http.requests], ["/page", "/page"])

    def test_redirect_is_rechecked_and_private_target_is_never_requested(self):
        """An allowed starting address cannot smuggle an internal target through a redirect.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        self.routes[("example.org", "/page")] = (302, {"Location": "https://127.0.0.1/secret"}, b"")
        with self.assertRaises(RetrievalError) as caught:
            self.reader.fetch("https://example.org/page")
        self.assertEqual(caught.exception.code, "blocked_address")
        self.assertEqual(len(self.http.requests), 1)

    def test_public_redirect_chain_is_retained_and_loop_is_rejected(self):
        """Provenance keeps redirect hops; a loop does not spend unlimited requests.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
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
        """Actual byte counting protects against missing lengths; compression is explicit
        unsupported input.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        self.budget.limits = RetrievalLimits(max_source_bytes=4, min_host_interval_seconds=0.0)
        for headers, expected in [({"Content-Type": "text/plain"}, "source_size_limit"),
                                  ({"Content-Encoding": "gzip"}, "encoded_response")]:
            with self.subTest(expected=expected):
                self.routes[("example.org", "/page")] = (200, headers, b"Too many bytes")
                with self.assertRaises(RetrievalError) as caught:
                    self.reader.fetch("https://example.org/page")
                self.assertEqual(caught.exception.code, expected)

    def test_request_and_total_byte_budgets_stop_work(self):
        """Document requests and bytes alone consume the configured shared limits.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        for limits, expected in [(RetrievalLimits(max_http_requests=0, min_host_interval_seconds=0.0), "request_limit"),
                                 (RetrievalLimits(max_total_bytes=4, min_host_interval_seconds=0.0), "total_size_limit")]:
            with self.subTest(expected=expected):
                with self.assertRaises(RetrievalError) as caught:
                    PublicWebReader(RetrievalBudget(limits)).fetch("https://example.org/page")
                self.assertEqual(caught.exception.code, expected)

    def test_application_pacing_and_http_429_are_retained_without_robots(self):
        """Keep application pacing and stop after HTTP429 without reading robots crawl delays.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        self.routes[("example.org", "/robots.txt")] = (200, {}, b"User-agent: *\nDisallow: /\nCrawl-delay: 99999\n")
        self.routes[("example.org", "/page")] = (429, {}, b"")
        self.budget.limits = RetrievalLimits(min_host_interval_seconds=2.0)
        self.reader.last_request["https://example.org"] = time.monotonic()
        with patch("projectclarity.retrieval.public_url_fetcher.time.sleep") as sleep:
            with self.assertRaises(RetrievalError):
                self.reader.fetch("https://example.org/page")
            self.assertTrue(sleep.called)
            # Pacing is now split into short cancellable waits. Their combined
            # duration honors the application interval, not the robots declaration.
            self.assertGreater(sum(call.args[0] for call in sleep.call_args_list), 1)
            self.assertTrue(all(0 < call.args[0] <= 0.25 for call in sleep.call_args_list))
        before = len(self.http.requests)
        with self.assertRaises(RetrievalError):
            self.reader.fetch("https://example.org/page")
        self.assertEqual(len(self.http.requests), before)
