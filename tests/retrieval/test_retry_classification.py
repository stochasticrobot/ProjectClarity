"""Check how the HTTP reader classifies failures and server-requested retry delays.

Real urllib3 exception types are supplied without making network requests. Tests
distinguish timeouts, certificate failures and ambiguous transport errors, then
check Retry-After seconds and dates. Classification must not turn an unsafe or
unknown failure into an automatic retry.
"""
import unittest
from unittest.mock import Mock, patch

import urllib3

from projectclarity.retrieval.public_url_fetcher import PublicWebReader, retry_delay
from projectclarity.retrieval.retrieval_limits import RetrievalBudget, RetrievalError
from tests.retrieval.retrieval_test_fixtures import public_dns


class RetryClassificationTests(unittest.TestCase):
    """Timeout, certificate and ambiguous failures must not share a retry policy."""

    def test_timeout_certificate_and_ambiguous_transport_errors_remain_distinct(self):
        """A generic HTTP-error handler must not hide certificate failures inside timeouts.

        Args:
            self: Unit test owner replacing only DNS and the network connection pool.

        Returns:
            None. Assert safe classifications, precharged request counts, and
            cleanup for each concrete urllib3 exception. No socket is opened.
        """
        errors = [(urllib3.exceptions.ReadTimeoutError(None, '/', 'private payload'), 'http_timeout'),
            (urllib3.exceptions.SSLError('private payload'), 'tls_failed'),
            (urllib3.exceptions.ProtocolError('private payload'), 'connection_failed'),
            (urllib3.exceptions.HTTPError('private payload'), 'http_failed')]
        for error, expected in errors:
            with self.subTest(expected=expected):
                pool = Mock()
                pool.request.side_effect = error
                reader = PublicWebReader(RetrievalBudget())
                with patch('socket.getaddrinfo', side_effect=public_dns), patch(
                        'projectclarity.retrieval.public_url_fetcher.urllib3.HTTPSConnectionPool', return_value=pool):
                    with self.assertRaises(RetrievalError) as result:
                        reader._request('https://example.org/page', 100)
                self.assertEqual(result.exception.code, expected)
                self.assertNotIn('private payload', str(result.exception))
                self.assertEqual(reader.budget.http_requests, 1)
                pool.close.assert_called_once()

    def test_retry_after_accepts_seconds_dates_and_rejects_invalid_values(self):
        """Keep server cooldowns distinct from the scheduler's default backoff.

        Args:
            self: Unit test owner checking parser behavior without network or sleeps.

        Returns:
            None. Assert numeric/date parsing and safe handling of missing/bad input.
        """
        self.assertEqual(retry_delay('3600'), 3600)
        self.assertGreater(retry_delay('Fri, 01 Jan 2100 00:00:00 GMT'), 3600)
        self.assertEqual(retry_delay('Wed, 01 Jan 2020 00:00:00 GMT'), 0)
        self.assertIsNone(retry_delay('not a date'))
        self.assertIsNone(retry_delay(None))
