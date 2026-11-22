# Nova output failures and bounded stage recovery

## Acceptance contract

A failed report leaf must not prevent other independent leaves from being offered
to the model unless cancellation, budget control or persistence failure stops work.
Malformed JSON, unknown references and unsupported citations remain failures.
Report selection and claim investigation may retry rejected output once, within
the same worker slot and existing budget. Attempts and raw replies remain separate;
this is a stage policy, not an SDK/transport fallback. A second rejection stays
failed. Valid evidence must not be discarded merely because a neighbour fails.

## Implementation

- `model_access/output_recovery.py` supplies opt-in two-attempt validation. Both
  attempts receive the identical full request; there is no hidden repair prompt,
  provider switch, unbounded retry or truncation. Transport/authentication failures,
  confirmed output limits, cancellation and persistence errors are not retried.
- Report split children run independently in item mode. A failed leaf retains
  usable fragments and does not skip its sibling. Legacy narrative mode keeps its
  prior strict behavior. Publication failures still stop the coordinator.
- Malformed report fields can trigger one reassessment. Earlier valid cited values
  survive; differences remain alternatives/conflicts. If both attempts fail,
  usable fields remain in a partial report and invalid fields remain quarantined.
  Exact-request cache reuse reconstructs preserved contributions from raw attempts.
- Required report lists and object-property restrictions are clearer in the prompt.
  Missing `unused_fact_ids` is not silently defaulted to an empty list.
- `SelectedField.value` may explicitly be null only when the field is insufficient.
  Such a field is audited and published as unknown with no borrowed citations.
  Supported/conflicting null values, wrong types and missing keys remain invalid.
- Claim citations can restore a unique whitespace-only match to exact source
  characters in the named passage. Each conversion records the original quote,
  passage ID and source span. Words, punctuation, case and passage IDs are not
  corrected. Ambiguous or wrong-passage matches fail. Original model text remains
  saved even when the validated decision uses restored source whitespace.

Existing omitted-fact recovery is separate and remains bounded to one targeted
pass. Its requests receive the same two-attempt output policy, so one fact can be
in the original assessment and one omission pass, each with at most two generation
attempts. Pre-request sizing may split either group but never truncates a fact.
All requests use the shared budget. A final partial report is still possible;
optional explicit retry remains available rather than promising automatic success.

## Evidence (2026-11-22)

- Full suite: **230 tests run, 226 passed, 4 database-dependent skips**.
- Production HTTP regression: a repeatedly failing nested leaf is tried twice,
  while every independent input is dispatched and accepted siblings survive.
- Regression checks cover cancellation before retry, no transport/truncation retry,
  write failure, separate validation outcomes, retained contributions, absent
  insufficient fields, exact whitespace restoration and negative citation cases.
- Five bounded live cases used eight generation calls (ceiling ten), reusing saved
  evidence. The initial live checks fully passed two cases; remaining failures
  were omitted input, explicit insufficient null values, and citation mismatches.
  Those failures are retained, not replaced with a favourable rerun.
- After the null/layout fixes, offline replay of each first live reply passes the
  structural checks for four cases. One still omits a fact and requires the ordinary
  targeted omission path. Wrong-passage later replies remain invalid. This is NOT
  five live successes under the final code, nor a full live successful report.
- Network-blocked production HTTP retry on a temporary copy offered **178/178**
  input facts, proving the skipped-sibling bug is removed on this real input.
  Responses unavailable for the new exact partitions deliberately failed: the
  replay remained partial (54 facts accounted, 124 unreviewed), not fabricated
  successful output. All **856** original files retained their hashes.

Private artifacts: `runs/nova-recovery-live/965a312dbe7949ef9450e86427f9e2fc/`.
This work does not establish semantic accuracy or redesign report types. No user
research was retried and no new sources were acquired by these checks.

## Local activation

After checking that no research or graph worker was active, the existing local
`projectclarity.service` was restarted. GET checks confirmed both previous partial
report attempts remained unchanged and the app responds on port 8775. No user
report retry was submitted. Prompt/schema changes alter request identity, so a
future explicit retry can require fresh assessments rather than reusing old-format
cached replies. This does not trigger new searches or upstream extraction.
