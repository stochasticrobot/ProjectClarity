"""Group focused checks for research sessions, display, storage, and workflow outcomes.

Purpose and responsibilities:
    Keep checks about managing and inspecting a research run together. For
    example, duplicate sources get one ID, a failed write preserves the old
    snapshot, and cancellation has its own exit code. Temporary files and
    substituted model/source calls avoid developer data and live services.
Typical use:
    Run ``python -m unittest discover -s tests/research -t . -v``. This package
    covers session-level behaviour; retrieval/ holds document acquisition checks.
"""
