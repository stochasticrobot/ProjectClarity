# Extract facts from document text

Converts source passages into structured entities and facts with supporting quotations. The document pipeline supplies each chunk; this package does not discover sources or decide whether a fact answers the research question.

## Reading order

- [knowledge_extraction.py](knowledge_extraction.py): Extraction schemas, requests, strict validation, and standalone CLI.
- [adaptive_chunk_sizing.py](adaptive_chunk_sizing.py): Per-document size ladder, promotion evidence, and exact-span recovery scheduling.
- [concurrent_extraction.py](concurrent_extraction.py): One worker request, grounding/repair, and provider outcome metadata.
- [fact_validation_and_repair.py](fact_validation_and_repair.py): Fact-level checks and one bounded correction attempt while preserving valid neighbors.

## Concurrency and recovery

Quote matching checks text presence, not factual truth. The standalone CLI uses
strict whole-response validation; the web workflow adds fact-isolated recovery.
These are not interchangeable accuracy benchmarks.

`concurrent_extraction.py` handles one isolated chunk and its optional repair.
The document coordinator schedules up to the saved per-run limit (default 6,
configurable from 1 to 16 in the question form's Settings disclosure). Extraction
and repair share the same worker slots. Workers send notifications through a queue;
only the coordinator writes session state, counters and the ordered knowledge index.
Stop prevents new calls and drains already-started work before finishing. Transport
failures remain visible. Other stages have their own concurrency controls.
Existing sessions without a setting default to 6.

## Adaptive chunk sizes (web workflow)

Dense lists can expand into many entities, facts and repeated quotes, while model
reasoning consumes the same output allowance. Real 750-character passages still
hit that limit, so the ladder now includes 325 characters. This is a bounded,
character-based workaround, not a guarantee of completeness or an optimal strategy.
Record-aware splitting, output-size prediction and model-supported reasoning/output
controls may be better approaches; they have not been explored or validated here.

Each document starts at **3,000 characters**. A provider-confirmed `max_tokens`
response causes the failed span and pending work to shrink to **1,500**, then
**750**, then **325**. The response allowance remains 8,192 output tokens. The failed request is
not resent unchanged; smaller replacement pieces use the ordinary shared model
client, worker slots and run-wide budget. Transport errors, invalid JSON and quote
validation failures do not trigger size recovery.

After **two clean replies below 60% of the output allowance**, pending ordinary
work can increase one level. Output usage includes reasoning. A failure, high-use
reply or unknown usage resets the qualifying streak; size changes also reset it.
Older in-flight successes cannot promote the new policy. Each document learns
independently, and pending recovery descendants only shrink, never grow back into
a previously failed span.

At 325 characters (or a shorter actual span that cannot be reduced on the ladder),
a truncated response remains an explicit unresolved source portion. Every recovery
lineage has at most three size reductions; there is no unbounded identical retry.
Boundary-aware splitting can create more than two children. Already-started work
finishes; Stop prevents new child calls, and budget pauses wait for the user's choice.

Failed parent receipts are retained. Only accepted leaves enter `knowledge.json`.
Source offsets and parent IDs preserve lineage; child links live in the saved run
summary because unstarted children may be repartitioned. Pending chunk totals can
change. Progress distinguishes split attempts, failed leaves and recovery calls.
Saved source reads preserve CRLF and Unicode for exact hashes and citations.

The standalone extraction CLI retains fixed chunks and strict validation. Adaptive
recovery does not change selection, triage, investigation, link-ranking or report
batching. Claim context uses offset-adjacent leaves rather than numeric chunk IDs.
Offline tests prove these controls, not live recovery rates or complete research.

## Targeted entity-omission review

After ordinary fact validation and rejected-fact repair, the web extraction worker
reviews accepted assertions with no object entity. These are **candidates**, not
proven errors: a numeric count or descriptive attribute can legitimately have no
object. This is not a headquarters-specific rule or a capitalization heuristic.

[entity_omission_repair.py](entity_omission_repair.py) sends those candidates and
their saved source chunk for at most one additional model call per chunk. The
[review prompt](../prompts/repair_entity_omissions.txt) asks for a missing entity
link, a legitimate literal value to keep, or an explicitly unresolved case. The
call shares extraction's worker slot, request/token budget and cancellation gates.
It adds latency and model usage; no review is sent when all assertions already
have object references. Failed reviews are recorded, not retried.

A repair may add an entity and set an assertion's previously null `object_id`.
It cannot rewrite its subject, predicate, literal value, statement or quotation.
The destination name must occur explicitly in both the statement and its supporting
quote, and the corrected assertion must pass the usual reference/quote checks.
Existing entities cannot be redefined; only referenced new entities are retained.
These checks constrain edits, but matching names and quotations do not independently
prove relationship meaning. Ambiguous decisions remain unresolved.

Chunk receipts retain the original model text plus `entity_omissions`, containing
pre-review records, candidates, exact request/reply, decisions, before/after patches
and unresolved IDs. The normal `parsed` view contains accepted repairs, so existing
selection and graph projection consume the same structured evidence. Unresolved or
failed reviews leave original facts available with `checks_partial` status. The
standalone extraction CLI is unchanged and does not perform this review.

This targets missing object references on already extracted assertions. It does not
recover entirely absent facts, detect incorrect existing links, resolve identities,
or expand one assertion into several geographic relationships. Existing saved runs
are not rewritten automatically. Offline replay must keep originals intact and
write any validation receipts separately; a corrected historical projection would
need an explicit saved repair/migration path, not edits to hashed extraction files.
