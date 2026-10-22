"""Group contract/credential unit tests and research-planning integration tests.

Purpose and responsibilities:
    Keep checks about turning a question into a plan together. For example,
    these tests reject broken goal links and verify that an accepted plan is
    saved. The example reply in fixtures/ is handwritten, not a model result.
Typical use:
    Run ``python -m unittest discover -s tests/planning -t . -v``. Unit tests
    isolate contracts and credential resolution; integration tests exercise
    the real module entry point and graph with provider HTTP replaced.
    Importing this package does not run the application or read any real key.
"""
