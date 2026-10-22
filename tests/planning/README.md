# Tests: planning

Check the planning request, answer schema, credentials and safe error explanations.

These tests help locate broken behavior. Offline fixture replies are not evidence of live model accuracy.

## Files

| File | Purpose |
| --- | --- |
| [test_plan_validation_and_credentials.py](test_plan_validation_and_credentials.py) | Unit tests for planning contracts, application-owned limits, and key resolution. |
| [test_planning_cli_integration.py](test_planning_cli_integration.py) | Exercise the production research CLI with an in-memory HTTP provider. |
| [test_planning_failure_diagnostics.py](test_planning_failure_diagnostics.py) | Check that rejected model answers explain their failure without leaking data. |

Run from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests/planning -t . -v
```

[Back to the test guide](../README.md)
