# Tests: claim review

Check claim review and the one-hop loop through the actual CLI, with external responses simulated.

These tests help locate broken behavior. Offline fixture replies are not evidence of live model accuracy.

## Files

| File | Purpose |
| --- | --- |
| [test_review_and_discovery_integration.py](test_review_and_discovery_integration.py) | Exercise the actual CLI, graph, SDK, parsers and file persistence without live services. |
| [test_quote_validation_integration.py](test_quote_validation_integration.py) | Prove original-source quote offsets survive the real CLI and one failed assessment does not discard a neighboring valid review. |

Run from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests/claim_review -t . -v
```

[Back to the test guide](../README.md)
