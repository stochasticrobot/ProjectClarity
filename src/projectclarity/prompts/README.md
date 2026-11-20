# Model-facing instructions

These text files describe individual research tasks in plain English. Application
code loads them and supplies the question, evidence, and structured response schema.
They do not control workflow order; the [graph](../workflow/research_graph.py) does.

## Current report prompts

- [prepare_report.txt](prepare_report.txt) states the requested answer contract.
- [prepare_report_batch.txt](prepare_report_batch.txt) filters one evidence batch
  into cited answer contributions, including partial information and unused facts.

These prompts are sent together by
[answer_item_selection.py](../reporting/answer_item_selection.py). Code assembles
the final items; the default path does not ask a model to merge or write a narrative.
Legacy report and merge prompts remain for historical regression paths only.

## Editing conventions

Keep instructions brief, concrete, and in ordinary English: explain the task,
its purpose, and how to handle uncertainty. Let response schemas carry formatting
constraints rather than embedding an elaborate output language in prose.

Prompt edits can change behavior and invalidate exact-request reuse. Review the
complete request, including Pydantic schema descriptions, and run the relevant
stage tests. Moving a prompt or editing documentation must not silently change
its contents. Tests of response shape do not prove semantic answer quality.

## Entity-omission review

[repair_entity_omissions.txt](repair_entity_omissions.txt) reviews accepted assertions
whose object reference is missing. It distinguishes literal attributes from named
entity relationships and requires uncertain cases to remain unresolved. The schema
permits only entity definitions and link decisions, not rewritten fact text. The
web extraction worker makes at most one such call per affected chunk.
