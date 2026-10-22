# Tests: finding what broke

The tests check that the parts of ProjectClarity work together and that invalid
inputs, missing evidence and exhausted budgets produce understandable outcomes.
They help us locate failures as we change code and later add infrastructure.

**Normal test runs are offline.** They use temporary files, synthetic credentials
and simulated provider/web responses. They do not spend model credits.

## Run the checks

From the repository root:

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```

This **automatically includes the combined five-person regression** in
`tests/evaluation/test_combined_membership_pipeline.py`. No separate evaluation
command or opt-in flag is needed. Its four checks cover successful combination
and deduplication, a wrong person despite a count of five, a budget stop, and
insufficient contributing sources. Re-scoring also checks altered evidence.
It runs the real CLI/graph with simulated source pages and model replies, so
normal test runs need neither an API key nor model credits.

`-v` prints each test's name and explanation. To check just one component:

```bash
.venv/bin/python -m unittest discover -s tests/extraction -t . -v
```

To rerun a particular module, use its dotted Python name:

```bash
.venv/bin/python -m unittest tests.monitoring.test_friendly_research_console -v
```

## Find the relevant folder

Each folder's README lists its files and what they check.

| Folder | What it checks |
| --- | --- |
| [llm_connection](llm_connection/README.md) | Credentials, model requests and provider failure handling. |
| [planning](planning/README.md) | Turning a question into a valid plan, before source contents are read. |
| [research](research/README.md) | Initialization, saved state, graph views, results and workflow failures. |
| [review](review/README.md) | Human **plan** clarification and continuing a paused run. |
| [interactive](interactive/README.md) | The actual terminal menu and question/answer interaction. |
| [retrieval](retrieval/README.md) | Reading sources, parsing documents, URL restrictions and download limits. |
| [extraction](extraction/README.md) | Passage handling, candidate extraction, quotation checks and private call records. |
| [claim_review](claim_review/README.md) | Model **claim** review, Python adjudication and one-hop link following. |
| [monitoring](monitoring/README.md) | Progress, prompt display, logging, status and cancellation. |
| [evaluation](evaluation/README.md) | Reference scoring, the automatically included five-person/multi-source regression, and evaluation-runner wiring; all use offline transports. |

There is no separate discovery test folder yet: the one-hop loop is covered
alongside claim review because that is the workflow it extends.

## What a passing suite does—and does not—mean

- **Unit checks** isolate a function or policy, such as locating a quotation.
- **Integration checks** exercise connected components. Several use the real
  CLI, LangGraph, provider SDK, parsers and saved artifacts, while replacing
  external HTTP/DNS responses with controlled fixtures.
- **Process checks** exercise terminal interaction, watching and cancellation
  across real local processes.

These checks establish application behavior under the tested conditions. They
do not measure live extraction accuracy, factual truth, review quality or search
relevance. Those need human-labelled evaluation data. They also do not prove
future AWS permissions, containers or deployed networking are configured correctly.

**Latest verification, 2026-10-22:** 240 offline tests passed in 135.724 seconds.
This includes all four combined-source/five-person checks, quote-matching/per-claim
isolation regressions and evaluator scoring/real-CLI wiring checks, alongside
the existing display and workflow tests.
No live model requests were made. A test count is not a
coverage percentage or a claim that every possible failure has a test.

## Optional live connection check

Run this only when you intend to contact the provider and potentially spend credits:

```bash
.venv/bin/python -m tests.llm_connection.live_connection_check
```

It prompts privately for a key and makes one bounded request. It is not discovered
by the normal suite and does not run on import. Live connectivity does not establish
research accuracy.

## When adding tests

- Name files `test_*.py` for the behavior they check; keep `__init__.py` so recursive
  unittest discovery can enter each package.
- Give each file a plain-language introduction and each test an explanation of
  what it proves. Explain what fixtures replace and what remains real.
- Never put real keys or private research material in fixtures.
- Prefer checks of meaningful behavior over frozen implementation details.
- Pydantic class docstrings and field descriptions can become model-visible
  schemas. Use ordinary comments for developer-only explanations.

[Back to the project overview](../README.md) · [Naming conventions](../docs/file-naming-guide.md)
