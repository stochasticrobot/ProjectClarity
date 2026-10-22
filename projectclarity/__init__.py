"""Application package for ProjectClarity's command-line researcher.

Purpose:
    This folder contains the code that takes your research question and produces
    a plan for you to review. For example, asking who studies AI security in
    Canada should produce smaller questions and suggested ways to investigate.
    Normal runs then collect text from supplied sources. With --extract, the
    program also proposes entities and claims and checks their quoted evidence.
    --review-claims adds a separate model assessment and Python adjudication.
    These remain candidates, not published facts or a final answer. --follow-links
    explicitly permits one observed-link hop on supplied URL origins, sharing
    the same budgets. Without it, only supplied sources are acquired. Web search
    remains unimplemented; --plan-only stops before any document retrieval.
Responsibilities:
    Mark this directory as a Python package. This file deliberately starts no
    workflow, reads no credentials, and performs no network or filesystem work.
Typical use:
    Run ``python -m projectclarity research --question "..." --plan-only``.
    Python enters __main__.py, which delegates to command_line.py. Import individual
    modules when reusing their contracts or testing their behavior.
"""
