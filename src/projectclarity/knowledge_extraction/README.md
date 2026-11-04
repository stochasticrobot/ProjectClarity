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
