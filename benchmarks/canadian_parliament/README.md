# Canadian Parliament extraction benchmarks

This directory contains two saved HTML pages and separate answer keys for checking
whether extraction recovers the member-of-Parliament (MP) and riding pairs shown
on each page. A riding is the electoral district a member represents. The pages
were captured on October 26, 2026; this is not a continuously updated membership list.

| Saved input | Expected current-as-presented MP–riding pairs |
| --- | ---: |
| `house_of_commons.html` | 337 |
| `wikipedia.html` | 337 |

Each input has its own `.expected.csv` and `.expected.json`. The JSON records source
coordinates, scope, historical exclusions and limitations. `sources.json` records
URLs, hashes and Wikipedia attribution. The Wikipedia key excludes 12 former-member
pairs. The two pages disagree in places: do not substitute one page's key for the other.

## Validate the reference files offline

From the repository root after installing dependencies as described in the root README:

```bash
.venv/bin/python -m unittest tests.evaluation.test_parliament_benchmark_references -v
```

This checks the answer keys against their saved pages. It needs no API key, makes
no model calls, and does not demonstrate model extraction accuracy. See the root
README's testing section for the full unit/integration suite and CI-ready runner.

## Run and score extraction

A live extraction run uses the configured Anthropic key and spends model credits:

```bash
.venv/bin/python -m evaluation.page_extraction_benchmark run \
  --source benchmarks/canadian_parliament/house_of_commons.html \
  --question "According to this page, list all current MPs and their ridings as of 2026-10-26; exclude former members." \
  --max-requests 150
```

The command prints its run directory. Substitute that path below to score locally
without another model call:

```bash
.venv/bin/python -m evaluation.page_extraction_benchmark score PATH_TO_PRINTED_RUN \
  --reference benchmarks/canadian_parliament/house_of_commons.expected.json
```

For the Wikipedia case, use `wikipedia.html` and `wikipedia.expected.json` instead.
Run one page at a time. Keep expected-answer files out of model inputs; only the
scorer should read them. The runner isolates extraction and quotation validation;
it does not exercise planning, search, claim review or final report completion.

Scores compare distinct `(mp, riding)` relationships, not name counts. Inspect
missing and unexpected pairs as well as precision and recall. The strict
comparison normalizes Unicode NFC, case and whitespace, reverses explicit
Last, First display order and removes conventional parliamentary titles. The
optional source-equivalence comparison separately reports additional rules. Preserve each source's wording
rather than inventing aliases to force agreement. A matching reference score
measures fidelity to the frozen page, not independently verified current membership.
