# Automated verification

Tests exercise the application with temporary stores and deterministic substitutes
for external search, downloads, and model calls. They require no model credentials.

## Run from the project root

After [installing the application](../README.md#run-locally):

```bash
.venv/bin/python -m unittest discover -s tests -t . -q
```

Run a smaller group by replacing `-s tests` with `-s tests/unit` or
`-s tests/integration`, keeping `-t .`. The command exits nonzero on failure.
It is suitable for CI, but no hosted CI/CD or automatic deployment is configured.
Node.js enables JavaScript status-indicator checks; unittest skips those checks
when Node is absent. Browser-development dependencies are separate in
[requirements-dev.txt](../requirements-dev.txt).

## Find a representative test

| Area | Starting point |
|---|---|
| Real HTTP-to-graph routing | [test_research_graph.py](integration/test_research_graph.py) |
| Clarification and setup | [test_research_setup.py](integration/test_research_setup.py) |
| Acquisition and extraction | [test_document_pipeline.py](integration/test_document_pipeline.py) |
| Bounded concurrent extraction | [test_concurrent_extraction.py](integration/test_concurrent_extraction.py) |
| Explicit saved-stage recovery | [test_selection_resume.py](integration/test_selection_resume.py) · [test_triage_resume.py](integration/test_triage_resume.py) |
| Multi-round research and report-only retry | [test_research_rounds.py](integration/test_research_rounds.py) |
| Current item-report contract | [test_item_reporting.py](integration/test_item_reporting.py) |
| Usage accounting | [test_model_ledger.py](unit/test_model_ledger.py) |

[unit](unit/) contains focused checks. [integration](integration/) contains HTTP,
workflow, and multi-component checks, including some helper assertions.
[fixtures](fixtures/) holds static inputs; fixture presence does not imply an
executable benchmark runner exists for every dataset.

## Interpretation

Production-entry tests keep graph routing and validation real while replacing
external I/O. Passing them establishes those contracts, not live search coverage,
provider latency, independent truth, or general model accuracy. Historical report
fixtures explicitly exercising legacy behavior are not proof of the current
item-first reporting path. Developer-run experiments belong in
[testing](../testing/README.md), not this automated suite.
