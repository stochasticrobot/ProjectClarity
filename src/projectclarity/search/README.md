# Search for candidate documents

Runs prepared queries and returns candidate URLs, titles, and discovery notes.
A search snippet is a lead, not accepted report evidence.

- [search_execution.py](search_execution.py) executes prepared queries, records
  outcomes, and applies the bounded fallback policy.
- [web_search.py](web_search.py) communicates with search services and handles
  search-specific pacing and result normalization.

The first research round uses the finalized question's two prepared queries.
The existing policy can try Wikipedia once when the second search fails or finds
nothing. Search outcomes remain visible even when other queries succeed.

Search does not download result documents or own their processing pool.
[SourceCollection](../workflow/source_collection.py) hands its results to the
[document pipeline](../documents/README.md). Additional research rounds use ranked
links and do not repeat this search step.
