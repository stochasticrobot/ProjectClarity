# Build a cited answer from reviewed evidence

Turns the finalized question, answer requirements, and reviewed facts into a set of
answer items. The default path filters evidence in bounded model calls, then
assembles the final table and CSV in code. It does not write narrative summaries,
make model merge calls, or collect missing evidence.

## Reading order

| File | Responsibility |
|---|---|
| [report_generation.py](report_generation.py) | Load reviewed evidence, enforce final field/citation/accounting checks, save report and spreadsheet-safe CSV. |
| [item_reporting.py](item_reporting.py) | Coordinate the current item-first path using the shared bounded dispatcher. |
| [answer_item_selection.py](answer_item_selection.py) | Request and validate cited answer contributions, uncertainty tags, and unused facts. |
| [answer_item_assembly.py](answer_item_assembly.py) | Combine compatible exact-label contributions and preserve missing or conflicting fields without model calls. |
| [report_payloads.py](report_payloads.py) | Compact whole facts and shared evidence without prefix-truncating their contents. |
| [batched_reporting.py](batched_reporting.py) | Shared token preflight, task receipts, bounded omission recovery, and exact-request reuse; also retains the legacy builder. |
| [concurrent_report_selection.py](concurrent_report_selection.py) | Bounded workers, coordinator-acknowledged checkpoints, and deterministic result ordering. |
| [report_reference_normalization.py](report_reference_normalization.py) | Resolve only unambiguous case/outer-whitespace reference differences against supplied IDs. |

## Inputs and output contract

[Research rounds](../workflow/research_rounds.py) supplies the original investigation
store for one round, or a separate immutable cumulative store for multiple rounds.
Only reviewed report facts enter it. Original source stores, fact IDs, source-run
paths, per-round input hashes, and review coverage remain inspectable.

The short [answer contract](../prompts/prepare_report.txt) and
[batch task](../prompts/prepare_report_batch.txt) are sent together. Schema enums
bind references to actual supplied facts and requirements. Each input fact must
be used, explicitly unused, or left pending the bounded omission-recovery step;
missing coverage cannot silently become a successful final report.

Code fills omitted fields as unknown, combines repeated field contributions
without dropping their citations, and groups exact normalized labels with
compatible distinguishing details. Homonyms with different qualifiers remain
separate. Distinct values and known conflicts remain visible. This is not fuzzy
alias resolution, and repeated-looking real-world entities can remain separate.

## Request limits and concurrency

- Report generation calls require provider-counted input of at most **16,000 tokens**,
  including system instructions, messages, and response schema. Count failures block generation.
- Initial evidence groups contain at most **40 facts**. Packing uses an estimate;
  provider preflight can split whole-fact groups further. An indivisible oversized
  fact fails explicitly rather than being truncated.
- Current item-selection requests allow **16,384 output tokens** and a **180-second**
  timeout. Input and output limits are separate.
- `report_selection_concurrency` defaults to **6**, range **1–16**. A worker's splits
  and permitted missing-fact recovery share its slot. Final item assembly runs in code.

## Failure and retry

Tasks retain the exact request, evidence hash, raw reply, token count, validation
outcome, and checked output. Matching prior responses are revalidated on explicit
retry; changing model, prompt, schema, or inputs can invalidate reuse. Responses
that fail validation are not treated as successful merely because they are saved.

In the item-first path, a failed batch no longer blocks useful output from other
batches. A partial report lists unreviewed input IDs/counts separately from explicit
exclusions, exposes a retry button, and labels CSV coverage. Even a failed bounded
recovery retains its validated parent findings. If no evidence can be assessed at
all, the attempt remains failed rather than presenting an empty success.

Malformed or uncited fields are withheld while valid cited siblings survive.
Uncited fields explicitly marked insufficient become unknown, with their text kept
only in the task audit. Other invalid fields are retryable issues and make the
report partial. No citations are guessed or borrowed. The exact raw reply remains
saved. Legacy narrative reporting still requires every batch to pass. Stop prevents
new dispatch and saves in-flight responses as they finish. A server restart does not replay calls. **Retry report** starts only reporting
from the saved reviewed evidence; it does not repeat search, extraction, review, or
completed ranking. Missing or unusable work may require new model calls.

Valid citations take precedence over redundant exclusions after structural checks.
Unknown or ambiguous IDs still fail reference checks; an affected field is withheld,
not silently assigned a valid ID. Formatting normalization never changes source
prose or guesses a different fact. Original provider replies remain unchanged.

## Verification and legacy code

[Item-report tests](../../../tests/integration/test_item_reporting.py) exercise the
current filter/assembly contract through HTTP and synthetic answer cases.
[Round tests](../../../tests/integration/test_research_rounds.py) cover cumulative
reviewed evidence and report-only retry. These prove execution and structural
contracts, not independent truth or general model accuracy.

`report_scheme=legacy` is an explicit historical regression path. It uses the
older narrative and model-merge machinery, including
[report_merging.py](report_merging.py), [report_batch_recovery.py](report_batch_recovery.py),
and legacy prompts. Those tests do not establish current item-first behavior.
The standalone `testing/report_batch_regression.py` still uses a legacy-shaped
fixture caller and copies only one investigation's inputs; it is **not a supported
replay recipe for current multi-round item reports**. Use the maintained integration
tests for that path. See [developer experiments](../../../testing/README.md).
