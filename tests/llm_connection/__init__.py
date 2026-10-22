"""Group the standalone LLM connection smoke check and its offline regressions.

Purpose and responsibilities:
    Keep checks about reaching the LLM together. For example, this folder
    contains the explicit live connection command and tests of how it handles
    errors. Merely importing this folder never sends a request or asks for a key.
Typical use:
    Run ``python -m unittest discover -s tests/llm_connection -t . -v`` offline.
    Use ``python -m tests.llm_connection.live_connection_check`` explicitly for a real model call;
    that check is not the application's planning/retrieval workflow.
"""
