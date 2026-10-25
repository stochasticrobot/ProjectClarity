"""Preserve provider response classifications that DDGS otherwise reduces to emptiness.

The scoped engine adapter runs in the existing single-query subprocess. It does not
solve challenges, change endpoints, or select a different provider. No HTML is saved.
"""
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import time


def classify_response(status, text, headers=None):
    """Classify transport evidence without interpreting a block as an empty search.

    Args:
        status: HTTP status reported by the selected provider's client.
        text: Response body inspected locally for explicit challenge/empty markers.
        headers: Optional response headers used only to retain Retry-After delay.

    Returns:
        Bounded diagnostic fields with no response text, cookies or credentials.
        Unrecognized successful HTML remains unknown, not confirmed empty.
    """
    body = text.lower()
    challenge = any(marker in body for marker in
        ('anomaly-modal', 'challenge-form', 'bots use duckduckgo', 'g-recaptcha', 'h-captcha',
         'captcha-container', 'unusual traffic from your computer network', 'startpage captcha'))
    code = ('search_provider_blocked' if challenge or status in {401, 403} else
            'search_rate_limited' if status == 429 else
            'search_service_unavailable' if status >= 500 else
            'search_http_error' if status != 200 else None)
    result = {'http_status': status, 'response_kind': 'challenge' if challenge else
            'explicit_empty' if 'no-results' in body else 'unclassified', 'code': code}
    value = next((v for k, v in (headers or {}).items() if k.lower() == 'retry-after'), None)
    if value is not None:
        try:
            delay = float(value) if str(value).strip().isdigit() else parsedate_to_datetime(value).timestamp() - time.time()
            result['retry_after_seconds'] = max(0, delay)
        except (ValueError, TypeError, OverflowError):
            pass
    return result


@contextmanager
def observe_engine(provider, observations):
    """Wrap only the chosen DDGS engine inside its isolated worker to retain HTTP evidence.

    Args:
        provider: Already-validated installed engine name; no fallback is introduced.
        observations: Mutable list receiving bounded response classifications.

    Returns:
        A context manager restoring the engine registry even on failure. The wrapped
        request uses the same HTTP client and parser as DDGS; blocked responses are
        not parsed or retried. This registry override is worker-local, not thread-safe
        for multiple simultaneous searches within one process.
    """
    from ddgs.engines import ENGINES
    original = ENGINES['text'][provider]

    class ObservedEngine(original):
        """Retain status before the library can discard non-200 response bodies."""

        def request(self, *args, **kwargs):
            """Fetch once using DDGS transport and classify its response before parsing.

            Args:
                self: Selected engine instance with the original configured HTTP client.
                *args: Original request method and endpoint from the engine.
                **kwargs: Original query/body parameters and client options.

            Returns:
                HTML for an unblocked HTTP 200; otherwise None. Transport exceptions
                propagate to DDGS and the worker's existing error boundary.
            """
            response = self.http_client.request(*args, **kwargs)
            observation = classify_response(response.status_code, response.text, getattr(response, 'headers', None))
            observations.append(observation)
            return response.text if observation['code'] is None else None

    ENGINES['text'][provider] = ObservedEngine
    try:
        yield
    finally:
        ENGINES['text'][provider] = original
