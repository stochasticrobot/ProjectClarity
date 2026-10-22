# Tests: llm connection

Check model connection plumbing. The explicitly named live_connection_check.py is the only live smoke-test command here; do not run it as part of normal tests.

These tests help locate broken behavior. Offline fixture replies are not evidence of live model accuracy.

## Files

| File | Purpose |
| --- | --- |
| [live_connection_check.py](live_connection_check.py) | Explicit live connection check: one real model call through LangGraph. |
| [test_connection_credentials_and_errors.py](test_connection_credentials_and_errors.py) | Isolated unit checks for the standalone connection smoke check's input and CLI. |
| [test_connection_graph_integration.py](test_connection_graph_integration.py) | Offline integration checks for the standalone connection smoke workflow. |

Run from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests/llm_connection -t . -v
```

[Back to the test guide](../README.md)
