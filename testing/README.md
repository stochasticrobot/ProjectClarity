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

## Minimal live model tests

Use the provider and model configured in the project's `.env` (normal application
precedence applies: explicit options, process environment, dotenv, defaults):

```bash
# Offline preview: shows selected provider/model and the three cases.
.venv/bin/python -m testing.live_model_tests
# Real calls to that provider/model:
.venv/bin/python -m testing.live_model_tests --live
```

This opt-in suite makes **at most three generation calls**, once per case, with
768 output tokens and a 60-second request timeout each. It uses the production
registry, codecs, caller and receipts; no mocks, repairs, automatic retries or
fallbacks. It does not run as part of ordinary unit-test discovery.

| Live case | What is checked |
|---|---|
| `basic-types` | Correct count, string label and boolean, with strict types and no extra fields |
| `nested-null` | Nested list, unknown fields as JSON null, literal name `"null"` preserved |
| `source-extraction` | Real application extraction prompt/schema/validator; one known fact, exact source quote, correct subject/city and valid references |

A failed call, incomplete response, malformed structure, wrong answer or evidence
mismatch fails its case; missing credentials never turn into a skip. Exit status
is 0 only for a preview or all live cases passing; failed live checks return 1.
Results and private production receipts are saved in a fresh UUID directory under
`runs/live-model-tests/`. Summaries record provider, model, contract fingerprint,
suite hash and per-case outcomes. Full responses and usage remain in the receipts;
terminal output does not print provider exception bodies or credentials.

Overrides are supported, for example:

```bash
.venv/bin/python -m testing.live_model_tests --live \
  --provider anthropic --model claude-haiku-5-5
```

When overriding providers, supply its model too if `.env` pins another provider's
model. Future registered text/JSON providers use the same harness; this does not
implement Ollama. The checks establish only a tiny output-contract sample, not
semantic reliability, full research-workflow success or production deployment.
The extraction check is intentionally narrow and can reject harmless rewording.

For the longer production extraction-worker checks, including bounded repair and
omission review, use `testing.provider_structure_check`; see
[structured-output evidence](../docs/STRUCTURED_OUTPUT.md). Its older development
and held-out fixtures are regression samples now, not fresh held-out evidence.
