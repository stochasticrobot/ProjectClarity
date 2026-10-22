# Does the researcher find the right members?

This is our first small **quality-evaluation pack**, separate from the software
unit tests. We give the normal researcher a question and sources, then compare
its saved relationships with an independently prepared answer key.

The researcher never receives that answer key. The answers were checked against
official pages and Wikipedia on **2026-10-22** and are **source-verified by the
assistant**. No user review is required. The [verification record](cases/reference_validation.md)
explains the checks. This is not human-labelled data or the planned 100-case benchmark.

## The built-in cases

| Case ID | Question scope | Expected people |
| --- | --- | --- |
| `canada_northern_mps` | Sitting Canadian MPs representing Yukon and Nunavut | Brendan Hanley; Lori Idlout |
| `us_vermont_house` | Sitting US House member representing Vermont | Becca Balint |
| `us_vermont_senate` | Sitting US senators representing Vermont | Bernie/Bernard Sanders; Peter Welch |
| `combined_five_members` | Combine the above geographic scopes in one research run | All five people above, each counted once |

These are complete answers **within those small geographic scopes**, not full
Canadian or US rosters and not a representative accuracy sample. Historical
names in the supplied pages test whether the researcher confuses former and
current members. The US cases also distinguish the House from the Senate.

Sources and reference notes are in [the case file](cases/legislature_membership.json).
Canada uses [House of Commons profiles](https://www.ourcommons.ca/Members/en/111109)
and Wikipedia. The US uses the [House Clerk](https://clerk.house.gov/members/B001318),
the [Senate's Vermont page](https://www.senate.gov/states/VT/intro.htm), and
[Wikipedia's Vermont delegation page](https://en.wikipedia.org/wiki/Vermont%27s_congressional_delegations).

**Source check:** all four Canadian inputs and both US House inputs were acquired
using our production retriever. The Senate's official page was viewable through
web research, but our retriever could not obtain its robots file and refused to
fetch the page. The Wikipedia input was acquired. The Senate case deliberately
retains that recorded limitation; do not interpret it as a model-quality failure
or bypass the crawler's access policy. Availability can change on later runs.

## Try it

From the repository root with the virtual environment activated:

```bash
# Free: list cases or inspect a question, sources and reference answers.
python -m evaluation list
python -m evaluation show canada_northern_mps

# Live: one actual research run; this can spend model credits.
python -m evaluation run canada_northern_mps --max-requests 20
python -m evaluation run us_vermont_house --max-requests 20

# Built-in multi-source test: one run combines four pages and checks all five people.
python -m evaluation run combined_five_members --max-requests 30
```

Run one case at a time. Add `--show-prompts` to inspect actual model inputs/replies.
The default output cap is 4096 tokens per call. Request limits are ceilings,
not promises of completion or currency caps. No live LLM evaluation was run while
preparing this pack; synthetic transport tests establish wiring, not accuracy.

Experiments go under `.projectclarity/evaluations/CASE_ID/EXPERIMENT_ID/`:

- `reference_suite.json`: the frozen answer key, verification record and source notes used for this experiment.
- `experiment.json`: reference fingerprint and requested budgets.
- `sources.txt`: URLs passed to the real CLI.
- `runs/RUN_ID/`: normal research artifacts, source snapshots and private call traces.
- `score.json`: matched, missing and unexpected relationships, plus failures.

To re-score existing output without another model call:

```bash
python -m evaluation score CASE_ID PATH/TO/runs/RUN_ID \
  --reference-suite PATH/TO/reference_suite.json
```

A different question or source list is rejected rather than scored against the
wrong answer key. The three original exploratory cases return the research exit
code without an accuracy threshold. The combined case is an explicit acceptance
test: `run` and `score` return nonzero if its checks fail or work is incomplete.

## What does the five-person test require?

The combined case uses four pages: the official Hanley and Idlout profiles, the
official Balint profile, and Wikipedia's Vermont delegation. The latter contains
the senators and overlaps with the Balint profile, so source results must be
combined and duplicate people counted once. The known robots-blocked official
Senate page is not an input to this case; it remains part of the independent
answer-key verification and the separate Senate case.

A **PASS** requires:

- Exactly Brendan Hanley, Lori Idlout, Becca Balint, Bernie/Bernard Sanders and Peter Welch.
- All ten expected reviewed relationships, with no unexpected reviewed membership/representation relationships.
- Supporting evidence contributed by at least two distinct source URLs; fetching two pages alone is insufficient.
- Completed acquisition, intact saved source text, extraction and claim review.

The CLI prints matched, missing and unexpected people and the number of
contributing sources. `score.json` retains the detailed checks. Duplicate aliases
count once. A completed but incorrect answer is **FAILED**; a request limit,
unavailable source or unfinished review produces **INCOMPLETE**. Neither passes,
but incomplete work is not automatically an application bug. A pass is specific
to this reference comparison, not a guarantee of semantic citation faithfulness.

The question does not tell the LLM the expected names or the target count. The
answer key remains in the evaluator. This fourth case reuses the existing five
verified people; it is not additional independent ground truth.

The free, repeatable regression of the same production CLI/graph path **already
runs as part of the normal automated suite**:

```bash
python -m unittest discover -s tests -t . -v
```

There is no separate opt-in for that regression. To run only its four checks:

```bash
python -m unittest tests.evaluation.test_combined_membership_pipeline -v
```

That test substitutes artificial page content and model replies at the HTTP
boundary. It proves combination, deduplication, validation and failure handling,
not live model quality. The live command above uses actual sources and credits.

## What is measured?

Each expected person has two relationships: **MEMBER_OF** their chamber and
**REPRESENTS** their constituency/state. The research question explicitly asks
for these relationships; this is not a parliament-specific change to the agent.

The scorer compares structured relationships, with explicit name aliases, and
reports precision/recall/F1 plus actual matches, misses and extras. It separates:

1. Relationships with intact anchored evidence.
2. Relationships also adjudicated supported after model review.

Repeated findings count once. Just mentioning a person's name does not count.
Wrong-chamber and former-member relationships are unexpected results. Unknown
predicates or prose without structured relationships are listed as unscored;
missed expected relationships still reduce recall. Missing sources, changed
source hashes, exhausted budgets and unfinished extraction remain visible.

**Limits:** matching an expected triple does not establish that the surrounding
claim or quotation has the right meaning. Semantic correctness, citation
faithfulness, party affiliation, start dates and a final report are not scored
here. Inspect `candidates.md` and the case's interpretation notes. A small handpicked
pack cannot establish general research accuracy.

## Dates and reproducibility

The answer key is dated; a new live run downloads current pages. Later elections,
resignations or source edits can invalidate that comparison. Keep the original
run snapshots, re-score those for repeatability, and review evidence before
versioning a new answer key. Do not silently refresh expected names from the
model being evaluated. Scores preserve the verification metadata from their own
frozen suite; an older suite without it is reported as `not_recorded`, not silently
upgraded to verified. The runner does not yet replay frozen sources into a new
model run; it only re-scores saved runs without fetching again.

## Files

| File/folder | Purpose |
| --- | --- |
| [membership_reference_cases.py](membership_reference_cases.py) | Loads questions and dated answer keys; no research runs. |
| [membership_result_scoring.py](membership_result_scoring.py) | Compares saved structured relationships and checks source integrity. |
| [multi_source_membership_check.py](multi_source_membership_check.py) | Checks the exact roster and evidence from multiple sources; distinguishes wrong answers from incomplete work. |
| [research_case_runner.py](research_case_runner.py) | Starts one real CLI run or inspects/scores saved output. |
| [cases](cases/README.md) | The inspectable source list, reference facts and review notes. |
| `__init__.py`, `__main__.py` | Package/command entry points; importing them starts no research. |

[Back to ProjectClarity](../README.md) · [Evaluator regression tests](../tests/evaluation/README.md)
