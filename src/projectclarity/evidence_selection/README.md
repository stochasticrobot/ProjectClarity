# Select relevant evidence

Filters saved extraction facts against the finalized question and answer requirements. Selected records retain their original evidence; selection indicates potential usefulness, not truth or completeness.

## Reading order

- [relevance_selection.py](relevance_selection.py): Relevance requests, supplied-ID checks, and original-record selection.
- [evidence_selection_stage.py](evidence_selection_stage.py): Batch scheduling, saved selections, counters, and stage outcomes.

## Concurrency and recovery

`concurrent_selection.py` checks one isolated batch with the existing prompt and
validators. `evidence_selection_stage.py` schedules up to the saved per-run
`selection_concurrency` limit (default 6, range 1–16), including on older sessions
without a setting. Explicit saved values are preserved. One coordinator saves
receipts, counters and selected facts in original batch order. Cancellation drains
in-flight results without starting remaining batches; failures stay unreviewed.
Triage waits for the complete selection handoff. No automatic model retries.

Stopped selection can be resumed from its screen with an explicit concurrency
limit. The action is bound to the displayed attempt; stale clicks cannot restart
it. The old attempt is archived, extraction/index hashes and context must match,
and successful batch receipts are revalidated against the exact current request.
Only failed or unfinished batches need model calls. Prior receipts are never
overwritten; new receipts record reuse provenance. Resume is blocked after
downstream triage has started to avoid invalidating existing reviews.
