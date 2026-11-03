# Document collection and processing

Turns an explicit list of document paths into saved source bytes, parsed text,
extraction receipts, and a usable-fact index. The default reader supports web URLs
containing HTML or text-bearing PDFs; accepting a local path in the pool does not
implement local-file reading.

## Reading order

1. [document_pool.py](document_pool.py) — collect paths, combine exact duplicates,
   and retain titles and origin notes without knowing how they were discovered.
2. [document_pipeline.py](document_pipeline.py) — coordinate downloading and bounded
   concurrent extraction, save progress, and publish `knowledge.json`.
3. [web_acquisition.py](web_acquisition.py) — URL/network checks, robots rules,
   redirects, download limits, parsing, and text chunking.
4. [document_links.py](document_links.py) — recover link text and destinations from
   saved HTML/PDF bytes without fetching those destinations.

## Inputs, outputs, and boundaries

[SourceCollection](../workflow/source_collection.py) supplies first-round search
results or later-round selected links. Processing itself does not run searches.
Each attempt gets its own directory containing a manifest, per-document source
and parsing artifacts, extraction receipts, and a document summary.

Unusable pages remain explicit outcomes rather than empty evidence. PDF parsing
has no OCR. In later rounds, recognized redirect duplicates retain acquisition
receipts but skip repeat extraction. Previously unknown aliases may still require
a GET before the duplicate destination is recognized.

The document coordinator owns shared counters and state writes; extraction workers
use the [extraction contracts](../knowledge_extraction/README.md). Stop preserves
completed work. Explicit document retry starts a retained new attempt, not a
promise to reuse every successful chunk from the previous attempt.
