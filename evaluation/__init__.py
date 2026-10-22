"""Opt-in research-quality experiments, separate from offline software regression tests.

Importing this package never starts a model call. Use `python -m evaluation list`
to see the dated cases; `run` deliberately invokes the real research CLI and may
spend provider credits. Reference answers are not sent to the research model.
"""
