"""Application package for ProjectClarity's command-line researcher.

Purpose:
    This folder contains the code that takes your research question and produces
    a plan for you to review. For example, asking who studies AI security in
    Canada should produce smaller questions and suggested ways to investigate.
    Normal runs then collect text from supplied sources. With --extract, the
    program also proposes entities and claims and checks their quoted evidence.
    --review-claims adds a separate model assessment and Python adjudication.
    These remain candidates, not published facts. The CLI prints an extractive
    provisional answer from reviewed evidence without a new model call. --follow-links
    permits recursive observed-link exploration across public sites, subject to
    saved URL policies, branch-progress gates and shared budgets. --search adds web search and
    source selection; --assess-coverage compares evidence with planned criteria.
    Without discovery flags, only supplied sources are acquired. --plan-only
    stops before searching or document retrieval.
    Broad graph capture is separate and requires an explicit auxiliary allowance
    in new CLI runs. Relative-depth search gating does not guarantee search gets
    a turn before a wide frontier exhausts the model budget.
Responsibilities:
    Mark this directory as a Python package. This file deliberately starts no
    workflow, reads no credentials, and performs no network or filesystem work.
Typical use:
    Run ``python -m projectclarity research --question "..." --plan-only``.
    Python enters __main__.py, which delegates to command_line.py. Import individual
    modules when reusing their contracts or testing their behavior.
"""
