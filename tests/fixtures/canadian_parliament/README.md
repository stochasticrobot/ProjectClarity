# Frozen Canadian Parliament extraction fixtures

Two saved HTML pages and separate answer keys support source-local checks of
member-of-Parliament (MP) and riding pairs. A riding is the electoral district a
member represents. The pages were captured on **October 26, 2026**; they are not
a continuously updated membership list.

| Saved input | Expected current-as-presented MP–riding pairs |
|---|---:|
| [house_of_commons.html](house_of_commons.html) | 337 |
| [wikipedia.html](wikipedia.html) | 337 |

Each input has its own `.expected.csv` and `.expected.json`. The JSON records source
coordinates, scope, exclusions, and limitations. [sources.json](sources.json)
records acquisition URLs, content hashes, and Wikipedia attribution/licensing.
Preserve those records with the fixtures.

The Wikipedia key excludes 12 former-member pairs. The two pages disagree in
places: compare each page with its own key rather than substituting the other
source's answer. A matching name with the wrong riding is an incorrect pair.

## Runner availability

The historical `evaluation.page_extraction_benchmark` runner and
`tests.evaluation.test_parliament_benchmark_references` module are **not included
in this checkout**. Earlier instructions referring to them or a
`benchmarks/canadian_parliament/` directory do not apply here. These files are
retained data fixtures, not a currently packaged end-to-end benchmark command.

For available commands, see [developer experiments](../../../testing/README.md)
and the [automated test guide](../../README.md). Neither the existence of these
keys nor the ordinary suite establishes extraction accuracy against these pages.

## Using the reference data

Keep expected-answer files out of model inputs. Evaluate distinct `(mp, riding)`
relationships, not name counts, and inspect missing and unexpected pairs as well
as precision and recall. Each JSON key states its comparison rules; do not invent
aliases to force agreement. Matching a frozen page measures source fidelity, not
independently verified current membership or complete research-workflow quality.
