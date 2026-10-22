# Test the evaluator, not the politicians

These offline tests verify that the evaluation runner uses the real research CLI
and that the scorer notices missing people, wrong chambers, duplicate results,
invalid evidence and source failures. They also check that the dated reference
audit covers every expected person and that old frozen experiments are not
retroactively marked verified. Invented fixture replies are not research
findings or measurements of a live model's accuracy.

All tests here, including the four combined five-person pipeline checks, are
automatically discovered by `python -m unittest discover -s tests -t . -v`.
The command below is only a shortcut for running this folder in isolation.

| File | What it checks |
| --- | --- |
| [test_membership_result_scoring.py](test_membership_result_scoring.py) | Expected/extra/missing relationships, explicit aliases, evidence integrity and separate review status. |
| [test_research_case_runner.py](test_research_case_runner.py) | Free inspection, real CLI execution with offline transports, frozen references, answer-key isolation and re-scoring without another call. |
| [test_combined_membership_pipeline.py](test_combined_membership_pipeline.py) | Four pages combine into five people through the real CLI/graph; wrong people, single-source findings and request limits cannot pass. |
| [multi_source_membership_fixtures.py](multi_source_membership_fixtures.py) | Tiny artificial pages and extraction replies, including overlapping names and aliases; not downloaded snapshots. |

```bash
.venv/bin/python -m unittest discover -s tests/evaluation -t . -v
```

[Evaluation usage](../../evaluation/README.md) · [All tests](../README.md)
