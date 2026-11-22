"""Bound opt-in output retries without weakening validation or hiding attempts."""
import unittest
from unittest.mock import Mock
from projectclarity.model_access.output_recovery import validated_output
from projectclarity.model_access.model_outcomes import ModelOutputStructureError, ModelOutputLimitError


class OutputRecoveryTests(unittest.TestCase):
    """Verify limits, accounting hooks and cancellation with no provider access."""

    def test_rejected_output_gets_one_identical_request_with_distinct_receipts(self):
        """Record a rejected reply and a later success without rewriting the first.

        Args: None.
        Returns: None; checks same request, separate stages and validation outcomes.
        """
        caller=Mock(side_effect=['bad','3']);before=Mock();attempts=[];request={'input':'unchanged'}
        result=validated_output(caller,'stage',request,'sid',int,before,attempts.append)
        self.assertEqual(result,3)
        self.assertEqual(before.call_count,2)
        self.assertEqual([a['status'] for a in attempts],['rejected_output','checks_pass'])
        self.assertEqual([c.args[0] for c in caller.call_args_list],['stage','stage-output-retry'])
        self.assertTrue(all(c.args[1] is request for c in caller.call_args_list))
        self.assertEqual([c.args[2] for c in caller.mark_validation.call_args_list],[False,True])

    def test_invalid_decoder_is_bounded_and_other_failures_are_not_retried(self):
        """Keep malformed JSON retryable but transport/truncation/write failures final.

        Args: None.
        Returns: None; verifies exact dispatch upper bounds for each failure class.
        """
        for error,expected in [(ModelOutputStructureError('bad JSON'),2),
                               (RuntimeError('network'),1),(ModelOutputLimitError('limit'),1)]:
            with self.subTest(error=type(error).__name__):
                caller=Mock(side_effect=error);attempts=[]
                with self.assertRaises(type(error)):
                    validated_output(caller,'s',{},'id',int,lambda:None,attempts.append)
                self.assertEqual(caller.call_count,expected)
                self.assertEqual(len(attempts),expected)
        caller=Mock(return_value='bad')
        with self.assertRaises(OSError):
            validated_output(caller,'s',{},'id',int,lambda:None,Mock(side_effect=OSError('disk')))
        self.assertEqual(caller.call_count,1)

    def test_cancel_before_retry_prevents_another_call(self):
        """Honor stop between invalid output and its proposed recovery attempt.

        Args: None.
        Returns: None; one original request remains recorded, no retry is sent.
        """
        caller=Mock(return_value='bad');before=Mock(side_effect=[None,InterruptedError('stop')]);attempts=[]
        with self.assertRaises(InterruptedError):
            validated_output(caller,'s',{},'id',int,before,attempts.append)
        self.assertEqual(caller.call_count,1)
        self.assertEqual(len(attempts),1)
