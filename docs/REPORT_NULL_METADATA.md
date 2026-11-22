# Report failure: optional metadata represented as null

## Diagnosis

The failed Nova report had 80 input facts. Three generation requests completed
normally with decoded JSON; none was truncated. All three failed item validation
because `distinguishing_detail` was null; one also returned null `order_key`.
The prompt described both as conditional metadata, but the schema required strings
without stating an absence representation. The remaining failed task receipts
were enclosing split tasks, not additional provider responses. Initial child
failures prevented their sibling branches from being assessed. No evidence was
accepted, so the application correctly kept the report failed rather than empty.

## Separate changes

The model-client/adapter comment pass changes no behavior: executable ASTs,
44 generated schemas and 16 prompt-file hashes matched before and after. Headers
now describe actual ownership and explicitly state that the Bedrock class is a
Nova Micro-only implementation, not generic Bedrock model support. Fixed Anthropic
output/context settings are explained without presenting them as universal limits.

The report fix **does change the output contract**: `SelectedItem` and its enclosing
`SelectedItems` schema now permit explicit null for the two metadata fields only.
Keys remain required. After strict item validation, those nulls become the existing
empty-string absence representation for grouping/sorting, with an audit of each
path. Labels, dispositions, field values, citations and all other types retain
their checks. No prompt text changed. Missing metadata keys are not filled.

The acceptance statement is: explicit absence in either optional metadata field
must not discard otherwise valid, cited report contributions, while malformed
identities, unknown citations and unsupported answer fields remain rejected or
quarantined under existing rules. Absent metadata must never infer a distinguishing
identity, ordering value or evidence reference.

## Evidence and limits

- All three original replies pass the corrected report boundary: 11 metadata
  conversions, no omitted facts within those three supplied batches.
- Network-blocked replay through the production HTTP report-retry route, on a
  temporary copy, produced a partial report with 7 entries and 25/80 facts accounted
  for. The other 55 remain unreviewed. Requests without saved replies deliberately
  failed rather than synthesizing new results.
- All 305 original run files retained their hashes. The actual failed session was
  not retried, changed or relabelled successful.
- Permanent regressions cover null versus literal text, required keys, invalid
  types/identities/references, and HTTP report assembly with audited conversions.
- This establishes structural recovery, not semantic correctness of the model's
  grouping, factual interpretation or field-to-evidence entailment. No live model
  retry was performed and the running app was not restarted for this fix.

The changed schema produces a different request fingerprint. Ordinary report retry
may make new model calls rather than reusing responses to the old schema. It still
uses saved reviewed evidence and does not rerun search, extraction or investigation.

Verification: 34 focused tests passed. The full suite ran 221 tests: 217 passed,
4 database-dependent tests skipped. Comment-only comparisons are distinct from
the intentional two-schema reporting change described above.
