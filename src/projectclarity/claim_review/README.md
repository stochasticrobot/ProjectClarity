# Review source support for claims

Checks whether selected facts are supported by the saved source material, then investigates flagged gaps or ambiguities. Reviews use the current round’s stored documents, not new web searches or independent corroboration.

## Reading order

- [claim_triage.py](claim_triage.py): Separate report-ready claims from claims needing investigation.
- [claim_evidence_collection.py](claim_evidence_collection.py): Collect original, neighboring, and keyword-matched saved passages.
- [claim_investigation.py](claim_investigation.py): Review the collected support and retain accepted, rejected, and unreviewed outcomes.

## Concurrency and recovery

`concurrent_triage.py` reviews one selected source group using the unchanged
full original chunk, prompt and validation rules. `claim_triage.py` schedules
bounded workers using saved `triage_concurrency` (default 6, range 1–16) and alone
writes ordered decisions and bucket counters. Stop drains already-started calls
without scheduling more groups. Existing resume revalidates exact saved replies;
previously failed batches remain unreviewed, not automatically retried. Earlier
stages and source artifacts are untouched. The question form exposes a separate
Concurrent triage calls setting; explicit saved values override the default.

`concurrent_investigation.py` collects a separate packet for each claim from a
single read-only saved-document snapshot. Investigation workers share the saved
`investigation_concurrency` limit (default 6, range 1–16). One coordinator keeps
original row order and publishes all active claims. Citations must identify the
supplied passage; the audited whitespace-only exception is described below.
Stop drains started calls and retains their results.

The investigation screen can resume stopped/partial/failed reviews before any
link-ranking or report stage exists. The action is bound to the displayed attempt.
Prior artifacts are archived, triage input hashes and question/requirements must
match, and successful accepted/rejected replies are reused only after their full
request matches newly collected evidence and citations validate again. Failed or
unfinished claims may make calls on this explicit resume; changed cached evidence
stays unreviewed rather than being silently accepted. No upstream stages replay.

Claim investigation opts into one output-validation retry per request. Both calls
consume the existing allowance and have separate receipts; failed transport calls
are not retried. A unique whitespace-only citation match can be restored to its
exact source span with an audit. Wrong words, ambiguous matches and wrong passage
IDs still fail. See [output recovery evidence](../../../docs/NOVA_OUTPUT_RECOVERY.md).

## Support-problem categories

Triage, investigation and profile identity assessment prompts request descriptive
`failure_categories`. The shared vocabulary lives in
[support_diagnostics.py](support_diagnostics.py): ambiguous location, name, date,
reference, relationship or scope; insufficient or conflicting evidence; unrelated
entity; and other support issue. Several labels may apply. The existing reason
states the specific missing detail. These are model judgments, not verified facts.

Triage records retain `triage_failure_categories`; investigation assessments
retain their own remaining `failure_categories`. Profile identity assessments
retain labels through uncertainty and exclusion records. Screens show available
labels next to explanations. Initial issues are not silently relabelled as
remaining issues after investigation.

This is diagnostic metadata only: no contextual assumptions, identity resolution,
new retrieval or acceptance/rejection rule is introduced. Technical request and
format errors remain separate. Historical replies without labels load as empty
(unclassified); they are not backfilled or interpreted as having no support gap.
Future prompts request an explicit empty list when no issue remains. Missing
labels remain tolerated for compatibility rather than making usable evidence fail.

Verification: 246-test offline suite completed (241 passed, 5 skipped);
15 focused support-category/profile integration tests passed after final retention
changes. Chromium verified a profile's visible ambiguous-name label; JavaScript
syntax checks passed for all four changed screens. No live-model behavioural
improvement is claimed, and existing research artifacts were not relabelled.
