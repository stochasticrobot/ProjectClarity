# Developer experiments

These tools inspect model behavior outside the web app. They are not application
dependencies or substitutes for the [automated regression suite](../tests/README.md).

| Tool | Purpose | Scope |
|---|---|---|
| [extraction_test_rig.py](extraction_test_rig.py) | Inspect requirements definition, extraction, or their handoff | Standalone strict whole-response extraction |
| [research_benchmark.py](research_benchmark.py) | Run frozen three-stage trials and score human-reviewed results | Benchmark inputs and rubric, not the complete web workflow |
| [report_batch_regression.py](report_batch_regression.py) | Historical offline report replay on copied inputs | Legacy-shaped reply fixture; not a supported current multi-round report replay |

## Preview without model calls

From the project root after [installation](../README.md#run-locally):

```bash
.venv/bin/python -m testing.extraction_test_rig combined tests/fixtures/business_excerpt.txt \
  --question "Which companies work on AI?" --dry-run
.venv/bin/python -m testing.research_benchmark run
```

Use each command's `--help` for options. The benchmark requires an explicit
`--live` option to make model calls. Live experiments use credentials and may incur
charges; preview output is not a measured model result.

The extraction rig validates whole responses. The web workflow additionally
retains valid individual facts and can request one bounded correction of rejected
facts. Scores from one path must not be presented as accuracy measurements for
the other. Keep answer keys out of model inputs and retain human-review judgments
when interpreting benchmark scores.

## Files and current limits

Frozen benchmark material lives in [benchmark_documents](benchmark_documents/).
Generated receipts belong in private `runs/` data and should not be committed.
The [Parliament fixture guide](../tests/fixtures/canadian_parliament/README.md)
describes a separate frozen dataset whose original runner is absent here.

The historical report replay copies only the current investigation's manifest and
report facts, not archived rounds, and uses the older report response shape.
Do not use it to claim current item-report or multi-round parity. For those paths,
run [test_item_reporting.py](../tests/integration/test_item_reporting.py) and
[test_research_rounds.py](../tests/integration/test_research_rounds.py) through unittest.
