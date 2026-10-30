"""Provide research evaluation tools and the offline regression-suite runner.

Most modules compare saved research outputs with explicit reference answers or
start separately requested live experiments. For example, python -m evaluation
list shows available dated cases without a model call. Live run commands can
spend provider credits; expected answers are kept out of research inputs.
The separate evaluation.regression_suite command runs offline software tests and
writes per-test results for local review or CI. Importing this package starts none
of these operations.
"""
