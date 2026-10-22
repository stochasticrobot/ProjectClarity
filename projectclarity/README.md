# The application: start here

ProjectClarity is an agentic research system based on LangChain.

This folder contains the program you run with `python -m projectclarity`.
You do **not** need to read every file to understand it.

## To use

Complete the [installation and API-key setup](../README.md#installation) first.
Requirements are **Python 3.12**, **Linux or WSL2**, and an **Anthropic API key
with available credits** for live research. Run all commands from the repository
root, not this package directory, with the virtual environment activated.

### Research web sources

Edit `source_urls.txt` to list your sources: one HTTP(S) URL per line. Blank lines
and lines beginning with `#` are allowed. The supplied file contains example
AI-security research sources.

```bash
python -m projectclarity \
  --question "Identify researchers and organizations studying AI security in Canada." \
  --urls source_urls.txt \
  --extract --review-claims \
  --max-requests 20 --max-output-tokens 4096
```

This plans the investigation, reads the supplied pages, extracts candidate
claims and reviews their supporting evidence. It does not follow links unless
you add `--follow-links`. Live research uses model credits.

### Research local documents

Use `--document` for text, HTML or text-based PDF files. Repeat it for multiple
files, or combine it with `--urls`:

```bash
python -m projectclarity \
  --question "Which organizations are named in this report, and what roles do they have?" \
  --document path/to/report.pdf \
  --extract --review-claims \
  --max-requests 20 --max-output-tokens 4096
```

Replace `path/to/report.pdf` with an existing file. Scanned PDFs require OCR,
which is not implemented. Extracted passages are sent to Anthropic for analysis;
local input does not mean offline processing.

### Common options

| Option | Purpose |
| --- | --- |
| `--question "…"` | Research question; required for a command-line research run. |
| `--urls FILE` | Path to a URL-list file, not an inline URL. |
| `--document FILE` | Local document path; repeat for additional files. |
| `--extract` | Extract candidate entities, relationships and claims. Requires at least 2 requests. |
| `--review-claims` | Assess support for extracted claims. Requires `--extract` and at least 3 requests. |
| `--follow-links` | Investigate one hop of observed links on the supplied URL origins. Requires `--review-claims` and at least 6 requests. |
| `--max-followup-sources N` | Limit additional sources to 1–3; default 2. |
| `--plan-only` | Stop after planning; omit extraction, review and link-following flags. |
| `--max-requests N` | Total model-call ceiling, 0–30; default 1. |
| `--max-output-tokens N` | Output-token cap per call, 128–8192; default 2048. |
| `--model MODEL_ID` | Override the configured Anthropic model; no automatic fallback. |
| `--output-dir PATH` | Run-storage directory; default `.projectclarity/runs`. |
| `--show-prompts` | Display model messages and replies, including source passages. |
| `--details` | Print the full graph, plan and extraction summary. |
| `--non-interactive` | Disable plan-review prompts; clarification can still leave a run paused. |

Request limits cover planning, extraction, review and link selection together.
They are not spending limits or guarantees of completion: a large source may
require several calls. A partial run retains completed work and records what
could not finish.

For the interactive menu, run `python -m projectclarity` without arguments.
For every option, including download limits, run:

```bash
python -m projectclarity research --help
```

### Inspect results

The CLI prints a run ID and saves files under `.projectclarity/runs/RUN_ID/`.
Start with `plan.md` for the research plan and `candidates.md` for extracted
claims and review statuses. These are working research artifacts, not a final
cited report.

Replace `RUN_ID` below with the ID printed by your run:

```bash
python -m projectclarity status RUN_ID
python -m projectclarity graph RUN_ID
python -m projectclarity inspect-calls RUN_ID
```

Inspection does not make model calls. For paused-plan review, cancellation and
detailed evidence inspection, see the [CLI guide](../docs/cli-guide.md).

## Read these in order

1. **[agentic_workflow.py](agentic_workflow.py)** — the overall workflow. Look at
   `build_graph()` to see the nodes and the connections between them.
2. **[research_planner.py](research_planner.py)** — how your question and source
   references become a planning request to the model.
3. **[retrieval/README.md](retrieval/README.md)** — how sources become saved text.
4. **[extraction/README.md](extraction/README.md)** — how text becomes proposed claims.
5. **[claim_review/README.md](claim_review/README.md)** — how claims are reviewed and assigned statuses.
6. **[discovery/README.md](discovery/README.md)** — how a useful link can trigger one more round.

The graph follows this general route; options can stop it earlier:

```text
Set up → Plan → Clarify if needed → Retrieve → Extract → Check quotes → Save
                                                                        ↓
                                                          Review → Assign statuses
                                                                        ↓
                                                             Consider useful links
                                                               ↓              ↓
                                                     one more retrieval     Finish
```

## Folder Organization

Major systems are in their own folder. Basically the program converts a query into a research plan,
and then executes the research plan by reading and crawling documents (URLs). 


| Folder | Plain-language job |
| --- | --- |
| [retrieval](retrieval/README.md) | Bring documents into the run and turn them into text. No LLM calls. |
| [extraction](extraction/README.md) | Ask what the text says, then locate the proposed quotations. |
| [claim_review](claim_review/README.md) | Ask whether the quotations support the claims, then apply Python rules. |
| [discovery](discovery/README.md) | Consider observed links and permit at most one follow-up round. |
| [providers](providers/README.md) | Connect tasks to the current model service and inspect selected calls. |

## Files directly in this folder

| File | Why it exists |
| --- | --- |
| `__main__.py` | Python’s entry point; forwards to the CLI. |
| `__init__.py` | Marks this as a package; does not run research. |
| `command_line.py` | Reads command options, starts the workflow and returns a shell exit code. |
| `agentic_workflow.py` | Defines the actual LangGraph nodes/edges and connects the component functions. |
| `research_planner.py` | Holds the planning instructions and builds the question/source-reference message. |
| `research_schemas.py` | Defines valid request, source, plan and session data—like shared forms. |
| `source_reference_loader.py` | Registers supplied URLs/files before reading their contents. |
| `session_storage.py` | Saves inspectable session snapshots and text artifacts. |
| `workflow_checkpoints.py` | Keeps LangGraph’s execution position for durable plan-review continuation. Not the same as a readable snapshot. |
| `planning_failure_diagnostics.py` | Explains rejected plan replies without dumping private error payloads. |
| `plan_renderer.py` | Formats the saved plan; does not decide research behavior. |
| `terminal_research_interface.py` | Owns the interactive setup menu and human question/answer flow. |
| `terminal_run_progress.py` | Explains live node/source activity and optionally shows model inputs/replies. |
| `terminal_run_summary.py` | Gives the short final outcome, unfinished work and next inspection commands. |
| `plan_review_contracts.py` | Defines the information exchanged when a human reviews a plan. |
| `plan_review_service.py` | Validates and submits a human decision to the paused workflow. |
| `plan_review_cli.py` | Connects review commands to that service. |
| `run_monitor.py` | Records events/status, owns the worker lock and observes cancellation. |
| `run_status_commands.py` | Implements status/watch/cancel commands. |
| `run_graph_visualization.py` | Formats the graph recorded from LangGraph; does not execute it. |

## Useful naming clues

- **`*_node.py`**: work performed by a named LangGraph step.
- **`*_messages.py`**: instructions and task input for a model call.
- **`*_contracts.py` / `*_schemas.py`**: the shapes of valid data, not instructions to execute.
- **`*_storage.py`**: saving data, not approving its truth.
- **`*_inspection.py` / `*_renderer.py`**: viewing saved information, not new research.
- **`_document_text_worker.py`**: the leading underscore marks an internal parser helper.

A **node** does work; a **conditional edge** chooses what comes next. For example,
`discover` is an edge label leading to `select_followup_sources`, not a separate
model call or file. The destination may decide there is no permitted useful work.

[Back to the project overview](../README.md)
