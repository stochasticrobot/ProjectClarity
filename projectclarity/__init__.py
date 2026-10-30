"""Implement ProjectClarity, a question-driven research application.

A run turns a question into a plan, reads permitted sources and optionally asks a
language model to extract and review claims. It retains quotations and unfinished
work so the terminal or local dashboard can present a provisional answer. Search,
link following, preliminary scope study and broad knowledge capture are controlled
by the request's options and allowances.

Run python -m projectclarity for guided terminal setup, or import individual
modules to reuse their helpers. agentic_workflow.py defines and executes the
LangGraph workflow, the connected sequence of research steps. This package
initializer itself starts no workflow and performs no credential or file access.
"""
