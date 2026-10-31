# ProjectClarity

ProjectClarity is an experimental AI research tool for questions that require
information from several documents or websites. You give it a question and can
supply documents or web addresses to start from. It can also search the web for
additional sources. Output is a report containing discoveries in CSV format,
notes, and an explorable relationship graph made of entities and relationships.

It breaks the question into research tasks, reads source material, and organizes
what it finds into a report with links to supporting evidence. You can inspect the
findings, see which questions remain unanswered, and continue saved research from
a browser dashboard or the command line.

For example, you could ask which Canadian researchers study AI security and what
they work on. ProjectClarity can look for relevant people, collect information
about their affiliations and research, and bring those findings together for you
to review. It is useful when gathering and checking information across sources
would otherwise mean repeatedly searching, reading and keeping notes by hand.

The report is a starting point for review, not a guaranteed answer. Information
may be missing, outdated or incorrectly interpreted; the supporting sources let
you examine the basis for a finding before relying on it.

## Contents

- [Installation](#installation)
- [Using ProjectClarity](#using-projectclarity)
- [Inspect or continue a run](#inspect-or-continue-a-run)
- [How to interpret results](#how-to-interpret-results)
- [Run the tests — no API key needed](#run-the-tests--no-api-key-needed)
- [Live evaluations](#live-evaluations)
- [Code orientation](#code-orientation)
- [Run knowledge explorer](#run-knowledge-explorer)

## Installation

Supported environment: **Python 3.12 on Linux or WSL2**, with `pip` and `venv`.
**Node.js** must also be on `PATH` for the complete test suite, which executes the
browser JavaScript checks. Node.js is not required to run the dashboard itself.
No GPU or separate database server is needed.

From the repository root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.txt
```

This installs the dependencies needed for both application use and offline tests.
Activate `.venv` again in each new terminal. You can run the tests immediately;
**do not configure a real API key just to test the code**.

For actual research, including analysis of local documents, an Anthropic API key
and model credits are required. Internet access is needed for provider requests
and any web search/downloads. Configure the key in your environment or a local
`.env` file:

```bash
test -e .env || cp .env.example .env
chmod 600 .env
```

Set `ANTHROPIC_API_KEY` in that file. Do not commit credentials or private run data.

## Using ProjectClarity

Each research run starts with a question. You can supply local documents or web
addresses, allow web searches, or combine these sources. The application creates
a research plan describing what it will try to find, then gathers and reviews
evidence for the answer.

You also set limits on the work, such as how many requests it can send to the AI
model and how long research may run. These limits are the run's **budget**: an
allowance for work, not a fixed price or a promise that the question will be fully
answered. Planning, reading and reviewing share the model-request allowance.
Research can stop with an incomplete report when a limit is reached. Model usage
is billed by the provider separately.

Choose the browser dashboard for a visual interface or the terminal for commands
you can repeat and script. Both use the same research engine.

### Browser dashboard

```bash
python -m projectclarity.dashboard
```

Open **http://127.0.0.1:8765**. The server binds to local loopback, not the public
Internet. Use `--port 8766` for another port or `--output-dir PATH` for another
run directory. The frontend has no build step.

Enter a question, select sources and set the research limits. **Plan & scope**
shows the plan, what the answer needs to contain and estimates of how much there
is to find; **Report** shows entries and unresolved evidence; **Targets** shows
items proposed for a selected goal. Selecting an old run only displays it and does
not feed its findings into a new run.

**Stop research** requests cancellation. An in-flight model call may still need to
return or time out. Closing the page or dashboard does not stop a research worker.
Live refresh pauses when the selected run stops or the tab is hidden; use **Refresh**
to pick up work started elsewhere while the dashboard is idle.

### Terminal

Run `python -m projectclarity` for guided setup, or provide a question directly:

```bash
python -m projectclarity \
  --question "Identify researchers and organizations studying AI security in Canada." \
  --urls source_urls.txt \
  --search --extract --review-claims --follow-links \
  --max-requests 200 --max-output-tokens 8192
```

Edit `source_urls.txt` to contain one starting URL per line; blank lines and `#`
comments are allowed. Omit `--urls` to start from search alone. For local sources,
use `--document path/to/report.pdf`, repeated for multiple documents. Text, HTML
and text-based PDFs are supported; there is no OCR or browser-script rendering.

The main options are:

| Option | Purpose |
| --- | --- |
| `--plan-only` | Produce a plan without retrieving sources; omit extraction flags |
| `--extract` | Ask the model for claims and result fields from acquired text |
| `--review-claims` | Separately assess whether passages support the proposed claims |
| `--search` | Permit web searches; free providers may fail or fall back |
| `--follow-links` | Permit selecting additional URLs observed in acquired pages |
| `--design-answer` | Define report sections, identity fields and evidence requirements before planning |
| `--coverage-study` | Sample sources early to propose coverage checks and optional scope estimates; default off |
| `--assess-coverage` | Compare collected evidence with the answer requirements |
| `--stop-when-satisfied` | Permit evidence-based early stopping; requires extraction and review; default off |
| `--max-requests N` | Shared model-call allowance, including retries |
| `--model-concurrency N` | Concurrent passage requests, 1–16; default 3 for new runs |
| `--model-retries N` | Extra attempts for temporary transport failures, 0–2; default 2 |
| `--model-timeout-seconds N` | Per-attempt timeout, default 60, maximum 300 seconds |
| `--max-pages N` | Source page/document cap; default 250 |
| `--official-domain DOMAIN` | Additional institution domain to prioritize, not a trust guarantee |
| `--knowledge-max-requests N` | Separate opt-in allowance for broad knowledge capture within the total limit |

Dashboard-created runs enable answer design by default; terminal runs require
`--design-answer`. Design consumes the shared allowance and needs room for planning.
The saved `answer_schema.json` describes the requested report, not research findings.
Older runs keep their recorded scheduling policy rather than being silently migrated.

Source restrictions come from `config/blacklist.txt` and `config/whitelist.json`.
`--whitelist-only` limits acquisition to allowed domains. Other controls cover link
depth, document switching and preliminary estimates. For all flags and defaults:

```bash
python -m projectclarity research --help
```

## Inspect or continue a run

Replace `RUN_ID` with the identifier printed by the application:

```bash
python -m projectclarity status RUN_ID
python -m projectclarity answer RUN_ID --details
python -m projectclarity review RUN_ID
python -m projectclarity cancel RUN_ID
python -m projectclarity resume RUN_ID
python -m projectclarity inspect-calls RUN_ID --attempt M1
```

`review` handles a paused clarification request. `resume` continues eligible saved
work without resetting allowances or replaying uncertain interrupted model calls.
`status` and `answer` do not make model requests. Detailed model-call inspection
can reveal private question/source text; its records are not ordinary progress logs.

Runs live in `.projectclarity/runs/RUN_ID/`, including `session.json`, `answer.json`,
`answer.md` and source records. Use `--output-dir PATH` consistently to change the
storage directory. New research starts fresh; only resuming continues existing work.

Recovery uses two kinds of saved state. `session.json` records research progress,
spent allowances and uncertain requests; `resume` uses it without automatically
repeating a request whose outcome is unknown. `workflow_checkpoints.sqlite` keeps
LangGraph's place for a human plan-review pause. New invocations save a checkpoint
when they finish, pause or unwind an exception, not after every research step.
An abrupt process kill may prevent that final checkpoint; it does not erase the
separate session journal. Existing checkpoint history is not automatically removed.

## How to interpret results

- **Claims** are statements proposed from source passages. A located quotation is
  not yet a semantic review of whether it supports the statement.
- **Report entries** combine fields for requested items. Designed answers combine
  observations only when all declared identity fields match. Missing identity stays
  in **Unassigned evidence**. Similar names do not automatically establish identity.
- **Goal Progress** shows proposed entry counts beside an estimated scope, such as
  `11 / ~8–200`. Entries can be unfinished; `?` means the total is unknown. Estimates
  are not exact percentage denominators or stopping targets.
- **Coverage assessments** judge evidence against answer requirements. Large evidence
  collections use a bounded summary tree; final requests contain inventory counts,
  not every report row. A completed summary is not a completed answer; all calls
  share the same allowance. Known-complete invalid summaries can receive one
  explicit correction; original caveats and uncertainty flags are preserved.
  Interrupted outcomes are not automatically replayed. Exhausted corrections
  leave coverage visibly blocked rather than repeatedly retrying unchanged work.
  These bounds limit request size; they do not establish semantic completeness.
- **Usage** distinguishes model requests from tokens. Missing token usage remains
  unknown; displayed known subtotals are not exact bills.

### Known limitations

Supported facts are separate from eligibility for the requested answer. New
designed-report runs check each entry's scope and requested date separately.
Section-wide evidence requirements, such as reconciling a complete roster, remain
whole-answer completion checks rather than conditions imposed on each person. An entry with supported fields remains pending if its
inclusion checks are unknown, unmet or conflicting. Older runs retain their
historical field-support counts and are labelled as not having these inclusion
checks; they are not silently re-reviewed. Runs created under an earlier inclusion
policy retain that policy until explicitly migrated; fresh runs use the corrected
entry-level checks. The dashboard distinguishes field-supported and qualified targets.

Partial observations can join when they share an explicit subject in the same
source version and extraction batch, with one supported, unambiguous identity.
Matching names alone do not establish identity. Background claims do not require
report rows. Structured repairs can close despite rewording when original field
values, qualifications and source evidence remain intact. More general paraphrase
resolution and timely execution of every useful repair remain limitations.

Review batches reserve response space based on claims, records, evidence size and
the configured output-token limit. This is an estimate, not a guarantee against
truncation. A single claim too large for that allowance is retained with an explicit
review failure without making a predictably oversized request.

Broad questions can produce overly restrictive answer requirements. Search fallback
can be narrower than the failed provider; zero results do not establish absence
from the web. Name and affiliation variants are not fully reconciled. Source metadata
and retrieval dates do not independently establish that a claim remains current.
Inspect the evidence, scope and unresolved work before relying on a report.

## Run the tests — no API key needed

Run commands from the repository root with `.venv` active. The suite uses Python's
standard **unittest** runner; there is no separate test service to start.

### Full offline suite

```bash
python -m unittest discover -s tests -t . -v
```

This discovers both unit and integration tests. Keep `-t .`: it makes the repository
the import root and avoids collisions between similarly named packages. The
command exits nonzero on test failures or errors. It requires Node.js for the
JavaScript checks as well as the Python dependencies installed above.

Unit tests check individual rules and helpers with controlled inputs. Integration
tests exercise combinations such as the command-line interface, workflow, parser,
saved files, local HTTP server and child processes. External model/search/page
responses are simulated; no real provider credentials or model credits are needed.
Some focused tests replace internal adapters or timing as well. Passing these
checks does not measure a live model's research accuracy.

### Run a focused group or one test

```bash
# All tests in one feature area (may include unit and integration tests).
python -m unittest discover -s tests/planning -t . -v

# A unit-test module: clarification context, with no model request.
python -m unittest tests.planning.test_prompt_context -v

# One unit test by its full module, class and method name.
python -m unittest tests.planning.test_prompt_context.PromptContextTests.test_legacy_answer_uses_only_the_original_plan_question -v
```

Tests are grouped by feature, not by a universal `unit`/`integration` marker:

| Directory | What it checks |
| --- | --- |
| `tests/planning/` | Plan validation, credentials, prompts and planning commands |
| `tests/retrieval/` | Document parsing, HTTP restrictions and retrieval limits |
| `tests/extraction/` | Passage boundaries, quotations and proposed claims |
| `tests/claim_review/` | Review outcomes, claim isolation and batched reviews |
| `tests/discovery/` | Search, link selection, fallback and branch progress |
| `tests/research/` | Scheduling, budgets, recovery, result accounting and reports |
| `tests/review/`, `tests/interactive/` | Human clarification and terminal interactions |
| `tests/dashboard/`, `tests/monitoring/` | Dashboard data, progress and worker control |
| `tests/knowledge/`, `tests/evaluation/` | Optional graph capture and evaluation/scoring tools |
| `tests/llm_connection/` | Offline connection checks; the live script is separate |

### CI-ready verification and saved results

Use the existing regression runner for a stricter pre-merge check:

```bash
python -m evaluation.regression_suite --jobs 4
```

It discovers the full suite and divides it among four worker processes. Use
`--jobs 1` for serial execution. Workers strip common API-key/token environment
variables and block non-loopback socket connections; local dashboard connections
remain allowed. This is a test guard, not an operating-system sandbox.

The command exits **0 only for a complete offline pass**, **1 for a failed or
incomplete gate**, and **2 for invalid command-line arguments**. Unlike ordinary
unittest, the gate also rejects skips, expected failures, missing results, missing
required regression cases and source changes during execution. Leave the source
unchanged until it finishes.

The printed output directory is under
`.projectclarity/diagnostics/regression-suite/`. It contains:

- `report.md`: readable summary.
- `results.json`: machine-readable verdict, counts, per-test outcomes and failures.
- `manifest.json`: discovered test IDs and worker assignments.
- `source-fingerprints.json`: hashes identifying the tested source files.
- `shard-*.log` and `shard-*.json`: worker logs and detailed results.

A future CI job can install Python 3.12, Node.js and the locked dependencies, run
that command, and retain the printed report directory even when the command fails.
No API secret is needed. This is the existing command-line integration point;
this README does not install a CI workflow or configure deployment. JSON reports
are available now; a JUnit XML exporter is not provided. Offline success is not
authorization to deploy or evidence of live research quality.

## Live evaluations

These are **separate from the offline test suite**. A `run` or `resume` command can
use model credits; scoring saved results does not call a model.

For isolated extraction of one frozen page, without planning, search or claim review:

```bash
python -m evaluation.page_extraction_benchmark run \
  --source benchmarks/canadian_parliament/house_of_commons.html \
  --question "According to this page, list all current MPs and their ridings as of 2026-10-26; exclude former members." \
  --max-requests 150
python -m evaluation.page_extraction_benchmark score PATH_TO_PRINTED_RUN \
  --reference benchmarks/canadian_parliament/house_of_commons.expected.json
```

The Wikipedia page and its own expected-answer file form a separate case. Expected
answers are not supplied to extraction. Scores compare person–riding relationships,
not just name counts, and measure fidelity to that frozen page rather than today's
membership. `score --source-equivalences` adds a separate comparison with explicitly
allowed source-backed variants; it does not overwrite the strict score.

To score supported final report records from a normal one-page research run, use
the actual field names in its `plan.json`:

```bash
python -m evaluation.research_result_benchmark PATH_TO_RUN \
  --reference benchmarks/canadian_parliament/house_of_commons.expected.json \
  --name-field full_name --riding-field riding
```

This command is read-only and reports incomplete records separately. To continue
an isolated extraction run, use
`python -m evaluation.page_extraction_benchmark resume PATH_TO_PRINTED_RUN`.
That resume command is distinct from resuming a normal research run by its run ID.

## Code orientation

The research workflow uses **LangGraph**, a library that connects processing steps
and applies rules to choose which step runs next. Read these files in order to
understand how a question moves through the application:

1. [Command-line interface](projectclarity/command_line.py) parses a request.
2. [Workflow definition and execution](projectclarity/agentic_workflow.py) registers
   LangGraph steps and connections, then starts or resumes the workflow.
3. [Scheduler](projectclarity/nodes/schedule_work/workflow_control.py) checks remaining
   work and limits. The [answer controller](projectclarity/nodes/schedule_work/answer_controller.py)
   chooses reading, review, assessment and discovery turns for designed-answer runs.
4. The step implementations in `projectclarity/nodes/` perform the selected work.
   [Model-call execution](projectclarity/providers/structured_model_call_execution.py)
   handles shared request mechanics; task instructions live in `projectclarity/prompts/`.
5. [Result accounting](projectclarity/research_results.py) and
   [report assembly](projectclarity/answer_schema/inventory.py) produce entries and
   counts. [Mapping checks](projectclarity/claim_review/report_mapping.py) separately
   track claims that have not supplied required report fields.
6. [Dashboard server](projectclarity/dashboard/server.py) launches workers and serves
   views of saved results; it does not define a different research workflow.

Model response classes and field descriptions can become part of the schema sent
to the model. They are not interchangeable with developer-only comments. Private
run data and local investigation reports are excluded from version control by default.

### Experimental extraction sizes

New runs default to **Medium**, with **Small** and **Large** also available in the
dashboard's Research settings, or use `--extraction-profile medium` on a research
command. These settings affect extraction only, not review/coverage output caps,
concurrency, source permissions, or the shared request/time budget.

| Extraction setting | Small | Medium | Large |
| --- | ---: | ---: | ---: |
| Maximum structured records/passage | 8 | 16 | 32 |
| Structured-text target (characters) | 3,000 | 6,000 | 12,000 |
| Hard passage ceiling (characters) | 12,000 | 24,000 | 48,000 |
| Serialized messages ceiling (characters) | 48,000 | 80,000 | 112,000 |
| Messages + response schema ceiling (characters) | 64,000 | 96,000 | 128,000 |
| Extraction output cap (tokens) | 8,192 | 16,384 | 32,768 |

These are application limits, **not verified model capabilities or proven quality
improvements**. Character counts are not tokens. Select settings supported by your
model; provider rejections retain their normal private error receipts and do not
silently downgrade or increase allowances. The final serialized input is checked;
overflow fails visibly without dropping source text or sending a partial request.

Omitting the profile selects Medium; an explicitly supplied `--max-output-tokens`
still caps extraction unless an extraction-specific cap is supplied. Saved runs
keep their resolved settings; older requests without settings retain legacy Small
packing and their saved output cap. Explicit profiles select their own extraction output
cap; `--max-output-tokens` still controls other tasks. Optional extraction-only
numeric overrides are `--extraction-records`, `--extraction-record-characters`,
`--extraction-passage-characters`, `--extraction-message-characters`,
`--extraction-input-characters`, and `--extraction-output-tokens`. The last option
can lower a preset's output cap for a different model. Smaller output caps may
require smaller passages too; no adaptive tuning is implied.

For example, add `--extraction-profile medium --extraction-output-tokens 8192`
to an existing research command to test medium packing with the original output
allowance. CLI help lists all controls. Python callers may supply an immutable
`ExtractionSettings.for_profile('medium', max_output_tokens=8192)` as the
`ResearchRequest.extraction_settings` field.

Resolved settings persist in the request, extraction inventory, configuration,
input receipts and call traces. Restart preserves the recorded partition and
rejects changed sizing against an existing inventory; use a new run for a different
profile. A shared deployment must restart its dashboard server after updating code
before the new selector can launch profile-aware workers. No live-model benchmark
is implied by the offline production-path tests.

### Source access policy

ProjectClarity does **not fetch or interpret robots.txt as an access policy**. Robots disallow rules,
crawl delays, and unavailable robots endpoints do not block source retrieval,
including redirect targets. There is no robots-policy opt-in or fallback lookup.
Domain allow/deny settings, public-network address checks, TLS verification,
application-owned pacing, HTTP rate-limit handling, and request/time/size budgets
remain in force. Search-provider errors and actual page HTTP failures remain
visible; removing robots checks does not guarantee that a site returns readable
content. Historical robots-related failure receipts and charged traffic are
preserved; existing failed sources are not automatically replayed.


### Run knowledge explorer

Click **Explore graph** beside **View report** on a run page. The explorer combines
all saved extraction entities, candidate relationships, structured record fields
and standalone claims with that run's broader-knowledge SQLite sightings. It is
not filtered to accepted report rows. Incidental entities and invalid/unreviewed
claims remain inspectable, with status and source quotations. Opening or refreshing
it makes no model calls and does not alter the original records or enable paid capture.

Search, filter by type/status, drag nodes, zoom, and click a node or connection.
**Focus this neighborhood** reveals adjacent discoveries; **Show all / reset filters**
restores the complete view. The node list is pageable, not a first-N graph cutoff.
Names appear on hover or selection, with **Show all names** available. Relationship
labels appear only on selected edges. **Layout** offers automatic, radial and
hierarchical views; hierarchy means connection distance, not organizational rank.
**Expand connections** adds neighbors to the current view; **Collapse this expansion**
removes only that expansion, preserving overlapping paths. **Back** restores scope,
filters, positions and viewport (up to 30 steps; refresh clears history).
The **Connection** filter uses exact saved labels, with record-field associations
separate from extracted relationships; it does not infer equivalent wording.

Solid edges are saved relationship proposals. Dashed edges are associations from
raw structured records, not inferred facts. Green connections are source-supported,
amber are unreviewed/other, and red dotted connections have invalid evidence
(overriding the association dash). Node colors/icons indicate entity type, not truth.
The inspector reports each evidence status separately, including mixed groups. Nodes include extracted entities,
record values and statements, so the node count is **not** a unique-entity count.
By default, matching organization labels group across sources and record formats.
Abbreviations require an explicit, unambiguous expansion in a saved intact quotation.
Complete structured records group within a goal only when every field agrees after
the existing name-format normalization; conflicting context and name-only people
stay separate. A person and structured records can share a node when an explicit
person-name field, source and claim reference identify exactly one matching person.
Complete-record matching composes with these attachments; names alone never bridge
people across sources. Original fields, even conflicting affiliations or invalid
records, stay individually inspectable and do not inherit a stronger review status.
The ×N badge counts original representations. Grouping is a reversible display
projection, not canonical identity
resolution. Expand a group to see its original records and sources, or turn off
**Consolidate matching nodes** to inspect the ungrouped graph. All original edge
IDs, evidence, review states and source records remain intact. The explorer cannot invent relationships
not captured in the saved evidence, nor display discoveries never extracted from a page.

The endpoint `/api/runs/RUN_ID/graph` is a read-only projection over the authoritative
saved session and run-scoped database sightings. Historical capture-disabled runs
still expose their extraction receipts without a database migration or paid replay.
Cytoscape.js is MIT licensed and bundled locally with its version/license/hash under
`projectclarity/dashboard/static/vendor`; the browser does not load a CDN dependency.

For graph ownership, data flow and developer checks, see the [dashboard guide](projectclarity/dashboard/README.md).
