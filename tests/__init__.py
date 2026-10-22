"""Root package for ProjectClarity's component-organized test suite.

Purpose and responsibilities:
    Let the test runner find checks in subfolders, such as planning and
    llm_connection, when you ask it to run the whole suite. This file itself
    runs no tests and changes no application settings.
Typical use:
    From the repository root, run
    ``python -m unittest discover -s tests -t . -v`` for all offline checks.
    Live checks have separate explicit commands and are not discovered tests.
"""
