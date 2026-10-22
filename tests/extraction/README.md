# Tests: extraction

Check passage handling, proposed claims, quotations and saved extraction-call inspection.

These tests help locate broken behavior. Offline fixture replies are not evidence of live model accuracy.

## Files

| File | Purpose |
| --- | --- |
| [extraction_test_fixtures.py](extraction_test_fixtures.py) | Provide a small invented source and the structured reply we expect to inspect. |
| [test_evidence_validation.py](test_evidence_validation.py) | Check the Python boundary between a plausible assertion and inspectable evidence. |
| [test_extraction_call_inspection.py](test_extraction_call_inspection.py) | Check that a developer can see what extraction actually asked and received. |
| [test_knowledge_extraction_integration.py](test_knowledge_extraction_integration.py) | Run extraction through the same CLI, LangGraph and SDK used by the developer. |
| [test_source_passages.py](test_source_passages.py) | Verify that extraction sees bounded pieces of the exact retrieved text version. |
| [test_source_quote_matching.py](test_source_quote_matching.py) | Check whitespace-only matching, Unicode offsets, unchanged wording and ambiguous repeated quotations. |

Run from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests/extraction -t . -v
```

[Back to the test guide](../README.md)
