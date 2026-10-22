# Tests: retrieval

Check local/web acquisition, parsing, permissions, receipts and shared download limits.

These tests help locate broken behavior. Offline fixture replies are not evidence of live model accuracy.

## Files

| File | Purpose |
| --- | --- |
| [retrieval_test_fixtures.py](retrieval_test_fixtures.py) | Small, deliberately artificial documents and an HTTP substitute for tests. |
| [test_document_text_extraction.py](test_document_text_extraction.py) | Check the text a reader actually gets from supported document formats. |
| [test_local_source_retrieval.py](test_local_source_retrieval.py) | Check local reading and source receipts separately from the graph and parser. |
| [test_public_url_fetching.py](test_public_url_fetching.py) | Check the boundaries around website access with no real network traffic. |
| [test_retrieval_budgets.py](test_retrieval_budgets.py) | Check retrieval limits without waiting, downloading, or starting a model. |
| [test_retrieval_cli_integration.py](test_retrieval_cli_integration.py) | Run retrieval through the actual command and LangGraph, without live services. |

Run from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests/retrieval -t . -v
```

[Back to the test guide](../README.md)
