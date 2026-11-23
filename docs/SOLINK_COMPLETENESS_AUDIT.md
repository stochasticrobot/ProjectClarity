# Solink profile completeness audit — 2026-11-22

## Scope and evidence

Read-only investigation of saved session
8ac102f5-85bb-402c-b7bd-4a0eff7aef34, report-b01e490cd0de.
No model calls, retries, source collection, application edits or run-artifact edits.
The 54 saved output attempts were replayed through the current production
validate_profile_batch function using the exact fact records from the saved report
manifest. Local audit output: /tmp/clarity-completeness-audit/results.json.

## Reconciliation

All 160 report-input facts appear exactly once across 30 terminal tasks: six
successful tasks account for 46 facts; 24 failed tasks leave 114 unresolved.
There are also 24 parent split receipts; these are not additional evidence batches.
Twenty-nine terminal tasks reached the model. One oversized single record did
not. Thus 159 facts were sent, not all 160. There is no missing sibling dispatch
in this attempt. Fifty-four model calls comprise 29 first attempts and 25 retries;
two retries succeeded and 23 failed again.

| Final blocking error | Failed terminal tasks | Unresolved input facts |
| --- | ---: | ---: |
| Invalid finding status (unknown, not_target, unsupported, ambiguous) | 12 | 52 |
| unused_fact_ids includes facts without identity assessments | 5 | 25 |
| Findings cite absent/nonmatching identity assessments | 4 | 20 |
| Identity quote absent from supplied source passages | 2 | 16 |
| One whole record exceeds input admission limit | 1 | 1 |
| Total | 24 | 114 |

Counts attribute each whole task to its final first-raised error. They do not
claim that every fact in a failed task is invalid, or that removing that error
would make the remaining reply pass. Replies can have multiple independent faults.

## Causes

1. Entity-profile validation is atomic: ProfileBatch validates the entire reply,
   followed by identity, finding and unused-fact checks that raise on the first
   defect. No independently valid profile contributions are returned on failure.
   Unlike the directory recovery path, the profile validator does not return
   quarantined-field diagnostics and usable partial output to the shared dispatcher.
2. Nova repeatedly fills empty sections with prose saying information is missing,
   using statuses the finding schema does not permit and often empty citations.
   The prompt says to omit unsupported findings, but that did not reliably govern
   these live replies. A malformed empty-section placeholder invalidates unrelated
   supported findings.
3. Missing identity assessments are inconsistently routed: uncited omissions can
   reach _recovery, but citing or listing an unassessed fact under unused_fact_ids
   raises before targeted omission recovery runs. No task in this run reached
   targeted omission recovery; all attempted recovery was identical-request retry.
4. validated_output retries the same complete request once, without conveying
   the failure or directing a smaller repair. This recovered two tasks but did
   not resolve 23 others. Splitting is triggered by input size, not output failure.
5. Invalid quotations include generated claim sentences substituted for source
   quotations, not just whitespace differences. These must remain rejected.
6. F155 is a large Clay product/navigation-list record. Its complete serialized
   request was estimated at 16,937 input tokens against a 16,000 limit, so it was
   not sent. This is distinct from exhausted model budget or output truncation.
   The saved budget is not paused: 337/750 calls, 1,375,724/8,000,000 known tokens.

## Customer examples

- F44 (Tim Hortons) appears in a supported customer finding in the returned reply.
  Its final task fails schema validation on unrelated missing-information
  placeholders labelled not_target. Other missing assessments also remain.
- F141/F45 (Five Guys) appear in a supported Buys finding with match assessments.
  Their task fails because other findings cite F62/F63 without assessments.
- F146/F152 (Hawaiian Bros) were supplied but their final task has missing identity
  assessments and findings referencing unassessed facts.

These establish loss of model-proposed customer findings, not independent
verification of all customers or every proposed sentence.

## Consequences and next repair scope

Keep strict citation and identity gates. Isolate validation to attributable
assessments/findings, retain independently checked contributions, and explicitly
track every unresolved fact for targeted bounded recovery. Empty-section
placeholders should not erase unrelated valid findings. Missing assessments need
targeted assessment, not conversion into exclusion. Failures should be reported
with terminal-task and fact denominators: the current failed_batches=6 counts
top-level workers containing failures, not the 24 failed terminal tasks.

This does not establish semantic correctness of mechanically accepted findings.
The separate Clay-to-Solink attribution failure remains open. No recovery count
or live reliability improvement is claimed by this audit.

## Follow-up attribution trace

The saved successful task e98f43db0314 accepts F131–F138. Its input explicitly
names Clay as the owner of those products; its model output marks those facts
match and asserts without evidence that Clay's offerings relate to Solink. It
then rewrites them into one Solink overview and three Solink section findings.
The current post-completeness-fix validator replay accepts all four contributions
with zero quarantined fields. This confirms that completeness recovery did not
fix semantic attribution.

The original selected record for F131 and its triage reason both identify Clay
Labs Inc. The report input still says "Clay offers the product Claygent AI";
compaction did not replace Clay with Solink. It does omit the unreferenced Clay
Labs Inc. definition, and the supplied Clay definitions inconsistently classify
it as Company and Product. Nevertheless the explicit Clay subject survived.
The decisive ownership rewrite occurs in report generation, after irrelevant
evidence passed selection and source-support triage.

A literal occurrence of "Claygent AI" proves neither identity with Solink nor
ownership by Solink. A proposed repair must separate direct subject attributes
from relationships involving other entities, require evidence for aliases or
ownership changes, and verify the actual generated claim rather than trusting
the generator's match label. Exact-name-only filtering is insufficient: it would
discard valid customer/founder relationships and aliases. No repair or live call
was performed during this follow-up.
