"""Distinguish a confirmed output ceiling from other model request failures.

The provider client still sends each request once. Extraction can use this specific
outcome to subdivide source text; unrelated errors must not trigger that recovery.
"""


class ModelOutputLimitError(RuntimeError):
    """The provider explicitly ended a response with stop_reason=max_tokens."""
