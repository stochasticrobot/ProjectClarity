# Tests: review

Check human PLAN review: pause, answer, reject and resume. This is different from claim_review/.

These tests help locate broken behavior. Offline fixture replies are not evidence of live model accuracy.

## Files

| File | Purpose |
| --- | --- |
| [review_cli_process.py](review_cli_process.py) | Run the production CLI in a fresh interpreter with external calls forbidden. |
| [test_plan_review_integration.py](test_plan_review_integration.py) | Prove that a real LangGraph interrupt survives across command-line processes. |
| [test_review_response_validation.py](test_review_response_validation.py) | Check malformed UI responses before they reach a resumable workflow. |

Run from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests/review -t . -v
```

[Back to the test guide](../README.md)
