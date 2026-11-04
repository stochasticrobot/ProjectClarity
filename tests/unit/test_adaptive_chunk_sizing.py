"""Check promotion edge cases without relying on thread-completion timing."""
import unittest
from projectclarity.knowledge_extraction.adaptive_chunk_sizing import AdaptiveChunkSizing


def source_fixture():
    """Build one long source with four initial spans and no PDF metadata.

    Args: None.
    Returns: Mutable source dictionary and its exact text for policy-only tests.
    """
    text = 'A'*12000
    return {'chunks': [dict(index=i, start=i*3000, end=(i+1)*3000, status='pending', facts=0)
                       for i in range(4)], 'pages': []}, text


def result(tokens=1000, status='checks_pass'):
    """Provide explicit accounting without inventing an actual provider call.

    Args: tokens: Reported output including reasoning, or None for unknown;
        status: Simulated extraction validation result.
    Returns: Minimal worker outcome consumed by the production sizing policy.
    """
    return dict(status=status, request={'max_tokens': 8192}, provider={'usage': {'output_tokens': tokens}})


class AdaptivePolicyTests(unittest.TestCase):
    """Falsify accidental promotion by stale calls, unknown use or interrupted streaks."""

    def test_stale_inflight_successes_cannot_promote_changed_policy(self):
        """Finish old large calls after shrinking and require new size-specific evidence.

        Args: None.
        Returns: None; removing the generation gate would make this regression fail.
        """
        source, text = source_fixture(); policy = AdaptiveChunkSizing(source, text)
        first, *stale = source['chunks'][:3]
        for chunk in [first, *stale]:
            chunk['status'] = 'extracting'
        first['status'] = 'failed'
        policy.observe(first, {'status': 'failed', 'output_truncated': True})
        for chunk in stale:
            chunk['status'] = 'checks_pass'
            policy.observe(chunk, result())
        self.assertEqual(policy.level, 1)
        self.assertEqual(policy.successes, 0)
        current = policy.pending(); current['status'] = 'checks_pass'
        policy.observe(current, result())
        self.assertEqual(policy.successes, 1)
        next_chunk = policy.pending(); next_chunk['status'] = 'checks_pass'
        policy.observe(next_chunk, result())
        self.assertEqual(policy.level, 0)
        self.assertEqual(policy.successes, 0)

    def test_unknown_high_usage_and_partial_results_reset_streak(self):
        """Demand two consecutive clean replies, not two successes separated by trouble.

        Args: None.
        Returns: None; tests the nearest integer above the 60% boundary and no false zero for unknown use.
        """
        for output in (None, 4916):
            source, text = source_fixture(); policy = AdaptiveChunkSizing(source, text)
            first = source['chunks'][0]; first['status'] = 'failed'
            policy.observe(first, {'status': 'failed', 'output_truncated': True})
            for value in (1000, output, 1000):
                chunk = policy.pending(); chunk['status'] = 'checks_pass'
                policy.observe(chunk, result(value))
            self.assertEqual(policy.level, 1)
            self.assertEqual(policy.successes, 1)
            chunk = policy.pending(); chunk['status'] = 'checks_partial'
            policy.observe(chunk, result(1000, 'checks_partial'))
            self.assertEqual(policy.successes, 0)
