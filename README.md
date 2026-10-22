# ProjectClarity

**An automated research assistant that keeps the evidence behind its claims.**

Give it a question and some sources. It plans the investigation, reads the
sources, proposes findings, checks the supporting passages and can investigate
useful links. You can inspect what it asked the model and what the model returned.

We are building this to develop practical skills in **agentic AI engineering,
LLM evaluation and observability, and AI/LLMOps/cloud**. It is currently a local
command-line application, not a finished hosted research service.

## What happens when you run it?

1. **Plan:** turn your question into smaller questions and a strategy. The first
   plan is made **before** reading source contents.
2. **Clarify if necessary:** pause if the plan genuinely needs your input.
3. **Read sources:** download supplied URLs or read local documents. Save original
   files, readable text and a record of where they came from.
4. **Extract:** ask the model to identify relevant entities, relationships and
   claims, together with supporting quotations.
5. **Check and review:** Python checks the quotations and references; a separate
   model task assesses whether the passage supports the claim. Python assigns statuses.
6. **Optionally investigate links:** choose useful links observed on the supplied
   pages, read one follow-up round, then extract and review that material too.
7. **Save the work:** keep the plan, documents, candidate claims, review decisions,
   call records and graph for inspection.

An **entity** is something named, such as a person or organization. A **claim**
is a statement we might use in an answer. A **node** is one step of the workflow;
an **edge** determines which step runs next. LangGraph connects these steps.

**A downloaded page is not verified truth. A quotation match is not semantic
support. A model-supported claim is not automatically published knowledge.**

## What works today—and what does not?

| Available now | Still to build |
| --- | --- |
| CLI research requests and human plan clarification | Web interface |
| HTML, text and text-based PDF acquisition | OCR and browser-rendered pages |
| Plan-directed extraction, evidence checks and claim review | Human claim-review/publication workflow |
| One explicitly enabled link-following hop | Web search and broad source ranking |
| Local saved evidence, prompts/replies, progress and graph views | Accepted knowledge database and a final cited findings report |
| Offline regression tests | Human-labelled quality evaluation and cloud deployment |

The model backend is currently **Anthropic**, accessed through LangChain.
LangGraph controls the workflow. Task prompts are separate from the provider
adapter, but other model backends are not implemented yet.

## Requirements

- **Python 3.12** with `venv` and `pip`. Development and testing currently use
  Python 3.12; other versions have not been verified.
- **Linux or WSL2.** The application uses Unix file locking and resource limits;
  native Windows is not supported. macOS has not been verified.
- **An Anthropic API key and available API credits** for live research. The
  offline test suite does not need a key or credits.
- **Internet access** to install dependencies, call the model and retrieve web
  sources. Research on local documents still calls the model.

Dependencies are pinned with hashes in [requirements.txt](requirements.txt).
[requirements.in](requirements.in) lists the direct dependencies, including
LangGraph, LangChain's Anthropic integration, Pydantic and document parsers.
No GPU, Docker, external database or cloud account is needed beyond Anthropic.

## Installation

Run these commands from the repository root (the directory containing
`requirements.txt`), using Bash on Linux or WSL2:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.txt
```

If Python reports that `venv` or `ensurepip` is unavailable, install your
distribution's Python 3.12 venv package before retrying.

For live research, create a local configuration file without replacing an
existing one:

```bash
test -e .env || cp .env.example .env
chmod 600 .env
```

Open `.env` in your editor and set `ANTHROPIC_API_KEY`. An existing environment
variable takes precedence over this file. `.env` and generated run data are
ignored by Git; keep real credentials out of tracked files.

Check that the CLI loads without making a model request:

```bash
python -m projectclarity --help
```

In a new terminal, return to the repository root and run
`source .venv/bin/activate` before using the commands below.

## To use

From this repository, with its virtual environment activated and the API key
configured, run:

```bash
python -m projectclarity \
  --question "Identify researchers and organizations studying AI security in Canada. Investigate useful links on the supplied sites." \
  --urls source_urls.txt \
  --extract --review-claims --follow-links \
  --max-followup-sources 2 \
  --max-requests 20 --max-output-tokens 4096
```

`source_urls.txt` is an editable URL list, not a curated list of established facts.
Use `--document path/to/report.pdf` for a local document; repeat it for more files.

- **Readable progress is the default:** see the active node, source, results and stop reason.
- Add **`--show-prompts`** to see actual model messages and replies as calls run.
  This includes private source passages; do not share the terminal transcript casually.
- Add **`--details`** for the full graph/plan/extraction output after the run.
- Omit **`--follow-links`** to stay strictly within supplied sources.
- Use **`--plan-only`** without the extraction/review flags to stop after planning.
- Run without arguments for the basic setup/plan-review menu. Advanced options use flags.

A long document may use several model calls. The request limit covers planning,
extraction, review and discovery together; it is a ceiling, not a promise that
all work will finish. A run marked **partial** preserves completed work and
explains what could not finish. Request/token limits are not currency limits.

For a concise option reference, see [application usage](projectclarity/README.md#to-use).
The [CLI guide](docs/cli-guide.md) covers acquisition limits, display modes and
continuing paused runs.

## Can it follow links?

**Yes, with `--follow-links`.** It chooses from links actually present in retrieved
HTML. It does not invent URLs or perform web searches. The current boundary is:

- One hop, with up to two follow-up sources by default (maximum three).
- Only the exact origins of the original supplied URLs; a subdomain is not automatically allowed.
- The same download and model-request allowances across both rounds.
- A link is a lead, not evidence about the unread destination.

To tell whether a run followed links, inspect **`discovery.json`** for selected
sources, then **`documents.json`** for their actual download outcomes. A selection
alone does not prove a download. Finding more people on the original page also
does not mean a link was followed.

## Where are the results?

Each run has a folder under `.projectclarity/runs/RUN_ID/`:

| Start with | What you will find |
| --- | --- |
| `plan.md` | The questions and proposed strategy |
| `candidates.md` | Proposed claims, supporting passages and review statuses |
| `documents.json` | What was actually acquired and where its files were saved |
| `discovery.json` | Link-selection reasons and why discovery stopped, when enabled |
| `workflow_graph.txt` | The graph and recorded node visits |

Inspect without making another model call:

```bash
python -m projectclarity graph RUN_ID
python -m projectclarity inspect-extraction RUN_ID --call S1_P1
python -m projectclarity inspect-calls RUN_ID --call review_S1_P1
```

`S1_P1` means source 1, passage 1. Use `inspect-extraction RUN_ID` or
`inspect-calls RUN_ID` without `--call` to list available calls.

## Where should I start reading the code?

**Start with the [application map](projectclarity/README.md)**, then
[agentic_workflow.py](projectclarity/agentic_workflow.py), which actually defines
and connects the LangGraph nodes. Each component folder has its own short README.

- [Application and folder map](projectclarity/README.md)
- [Documentation index](docs/README.md)
- [Tests and how to run them](tests/README.md)
- [Starter research-quality evaluations](evaluation/README.md): dated Canadian MP and US Congress cases, including a built-in test that combines four sources and checks for exactly five verified people. Run `python -m evaluation run combined_five_members --max-requests 30` for the live test (uses model credits); the guide also gives its free offline regression command.
- [Development plan](DEVELOPMENT_PLAN.md)
- [Current node status and future work](docs/research-workflow-roadmap.md)

Run offline checks with:

```bash
python -m unittest discover -s tests -t . -v
```

This includes the built-in multi-source/five-person regression automatically;
no separate evaluation command is required for the offline test.

These checks verify application behavior with simulated external services.
They do not establish live model accuracy or guarantee factual correctness.
