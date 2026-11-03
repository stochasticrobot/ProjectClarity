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
original row order and publishes all active claims; original citation rules and
prompts remain unchanged. Stop drains started calls and retains their results.

The investigation screen can resume stopped/partial/failed reviews before any
link-ranking or report stage exists. The action is bound to the displayed attempt.
Prior artifacts are archived, triage input hashes and question/requirements must
match, and successful accepted/rejected replies are reused only after their full
request matches newly collected evidence and citations validate again. Failed or
unfinished claims may make calls on this explicit resume; changed cached evidence
stays unreviewed rather than being silently accepted. No upstream stages replay.
