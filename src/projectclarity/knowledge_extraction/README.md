# Extract facts from document text

Converts source passages into structured entities and facts with supporting quotations. The document pipeline supplies each chunk; this package does not discover sources or decide whether a fact answers the research question.

## Reading order

- [knowledge_extraction.py](knowledge_extraction.py): Extraction schemas, requests, strict validation, and standalone CLI.
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
failures remain visible; this does not introduce automatic provider retries. Other stages have their own
concurrency controls. Existing sessions without a setting default to 6.
