# Research packet v1 — frozen benchmark policy

This is authored fictional source material, not retrieved real-world evidence.
The six chunk boundaries are fixed. All 24 source assertions are evaluation units;
compound assertions require the entire meaning, including identity and qualifiers.
Three questions use this same packet. The answer key and planner criteria are
for the evaluator only: they must never enter model requests. Source assertions
are source claims, not independent real-world truths.

## Relevance policy

`required` means useful evidence that should be retained for the question, not a
claim that its subject qualifies as a positive answer. Explicit non-AI work and
negative office claims are required counterevidence. Medical AI project identity
is required for the funding question even when no funding is supplied: it defines
the projects whose funding is being sought. Absence of a grant cannot be invented.

`optional` is defensible context, neither required for end-to-end recall nor a
false positive when selected. `irrelevant` contributes to none of the requested
information for that question. Each assertion's labels are fixed in answer_key.json.

The funding question's “these medical AI projects” means projects identified in
this packet, not the preceding trial's results. Trials are independent. Entity
IDs remain chunk-local. Lena Ortiz's affiliation and project are deliberately in
different chunks; selection can retain both as potential contributions, but the
benchmark does not claim that this pipeline resolves their identities globally.
The chef named Mira Chen must not inherit Dr. Mira Chen's research role.

## Review instructions

After a live run, edit its review.json (not the frozen packet or receipts).
Set `reviewer` to an accountable name or label. Consult each trial's upstream
plan.json and chunk receipts plus selection receipts; those contain the exact
requests, model outputs and supporting source text. report.md links to a
review-workbook.md showing the records in one place.

For every planner criterion: true means the generated requirements request that
information unambiguously; false means omitted or incorrect. Do not require exact
wording, or penalize additional reasonable requirements. Planner coverage is a
separate score; it is not a claim that poor planning caused a later omission.

For every extracted fact:

- `meaning_correct`: true only if subject, predicate, object/value and statement
  agree with the source on identity, direction, negation, date, amount, conditions,
  attribution and uncertainty. A positive predicate with a negative statement is
  false. A correct fact not listed as a separate gold unit may still be true.
- `evidence_complete`: true only if the stored exact quote supports the entire
  assertion including identity and qualifications. An exact but partial quote
  fails. Check quote entailment separately from statement correctness.
- `relevance`: required, optional or irrelevant for this question, applying the
  frozen policy to the fact's actual content. Do not make an incorrect but relevant
  claim “irrelevant” to hide an extraction failure: validity is scored separately.
- `notes`: explain ambiguous judgments or defects; do not loosen the key to make
  a run pass. Unresolved judgments remain null and semantic scores stay pending.

For every source assertion, replace `supporting_fact_ids: null` with a minimal
sufficient bundle of extracted IDs that collectively express the ENTIRE assertion,
including its qualifiers. One combined fact may support multiple gold assertions;
several split facts may jointly support one. Use [] when no sufficient bundle
exists. Choose a selected sufficient bundle if multiple equivalent bundles exist,
so redundant extraction does not create a false selection miss. IDs must come
from that assertion's own chunk. Invalid facts may be referenced to diagnose an
extraction defect, but count as neither captured nor retained.

Do not map only part of a compound assertion. Do not accept an invented relation,
changed negation or unstated identity. Alias equivalence is limited to explicit,
unambiguous same-person references inside the chunk; dates may use equivalent
unambiguous calendar formats. Other disputed variants remain pending review.

## Scores and denominators

- Requirements coverage: covered criteria / all fixed criteria for the question.
- Extraction coverage: fully captured, meaningful, fully evidenced source units /
  all 24 units, including irrelevant ones.
- Meaning accuracy: correct extracted records / all extracted records.
- Complete evidence: fully supporting quotes / all extracted records.
- Selection recall: selected relevant extracted records / all relevant extracted
  records (required or optional). This isolates selection from extraction validity.
- Selection precision: relevant selected records / all selected records.
- Selected valid relevance: relevant AND meaningful AND fully evidenced selected
  records / all selected records. This exposes faithfully copied extraction errors.
- End-to-end recall: required source units retained with complete valid support /
  all required source units. Optional assertions do not enter this denominator.
- Record integrity: automatic equality check of selected original records, entity
  context, requirements, research question and source-artifact digest.

Empty denominators are N/A, never 100%. The assertion ledger identifies extraction
unavailable, extraction missed/invalid, selection unavailable/missed, or retained.
A missing field is not proof of absence unless the source explicitly says so.
A failed prerequisite means incomplete workflow. Reviewed scores on such a trial
are diagnostic only; no aggregate “benchmark passed” or single accuracy is given.
Scores describe the checked sample, not held-out performance or Wizard approval.

## Reproducibility and scope

The run snapshots the source packet, questions, key and this policy; hashes code,
prompts and dependencies; retains the actual requests and returned model metadata;
and binds assessments to immutable evidence. Every reassessment writes a new
folder with a review snapshot, so previous scores remain inspectable. An
interrupted runner retains completed trial.json checkpoints but has no completed
benchmark manifest of trials; do not report it as complete. Resume is not implemented.

One full pass has at most 39 calls (3 plans, 18 extractions, 18 selections). Failed
planning/extraction and empty extraction results can reduce actual calls. No
retries, truncation repair, retrieval, web acquisition, cross-chunk entity merging,
claim validation, or answer writing is added. Offline fake-model tests prove
routing and scorer behavior only. They never establish live-model accuracy.

Three repetitions are configured with --repeat 3 --max-calls 117. Compare semantic
scores and assertion outcomes across all repetitions, not just their best run.
This packet is the development benchmark; it is not a held-out generalization test.
