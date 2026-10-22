# Bring source documents into the run

This folder downloads permitted pages or reads supplied local documents, turns
them into readable text and records where they came from. It uses Python, not an
LLM. Reading a page does not mean its statements are correct.

**Start with `source_retrieval.py`.** The graph calls it for each registered source.
Documents are stored under the run’s documents/SOURCE_ID folder, with original
bytes, text and a receipt. There is no OCR or JavaScript browser rendering.

## Files

| File | What it does |
| --- | --- |
| [source_retrieval.py](source_retrieval.py) | Coordinates fetching/reading, parsing and saving one source and its receipt. |
| [public_url_fetcher.py](public_url_fetcher.py) | Downloads public URLs while enforcing robots rules, pacing, redirects and network restrictions. |
| [retrieval_limits.py](retrieval_limits.py) | Owns shared download counters and limits, including cancellation checks. |
| [document_text_extractor.py](document_text_extractor.py) | Asks a bounded helper process to turn HTML, text or PDF bytes into readable text. |
| [_document_text_worker.py](_document_text_worker.py) | The internal parser process; also captures HTML links before navigation text is removed. |
| `__init__.py` | Marks this folder as a Python package; it starts no work. |

[Back to the application map](../README.md) · [Project overview](../../README.md)
