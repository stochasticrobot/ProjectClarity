# Folkly article: requested URL versus evidence URL

[Case study](README.md) · [Recorded provenance](provenance.json) · [Source list](sources.json)

## Which document supplied F437, F443 and F539?

The saved acquisition receipt records this sequence:

1. **Requested URL:**
   `https://aimagazine.ca/canadian-ai-spotlight/ai-startup-profiles/folkly-ai-and-the-quiet-revolution-in-cultural-archiving/`
   returned **HTTP 301**.
2. **Final URL:**
   `https://aimagazine.ca/canadian-ai-spotlight/folkly-ai-and-the-quiet-revolution-in-cultural-archiving/`
   returned **HTTP 200**, supplying 288,178 bytes of HTML.

The extraction and report citations use the final URL. The original `sources.json`
export showed only the requested URL, leaving this transition unexplained. That
export now includes both URLs, the recorded HTTP hops and the downloaded-content
hash. Its existing `url` field remains the requested URL; no citation was rewritten.

## Checks against the saved artifacts

- The saved HTML's SHA-256 matches the acquisition receipt.
- F437 and F443 trace to extraction chunk index 1; F539 traces to index 5.
- Each chunk artifact's SHA-256 matches the hash in the report's original fact ID.
- Both chunk records identify the same final URL and parsed-document SHA-256.
- The saved parsed text matches that document hash, and contains every source
  quotation attached to these three report facts.

[provenance.json](provenance.json) contains the hashes and per-fact check results.
These checks identify the document used by this run. They do not independently
verify the article's assertions. Full source bodies and private provider receipts
are not included in the public case-study export.

## Why current pages may not settle historical provenance

A reviewer reported different content and authors at the two paths. A subsequent
web-tool check on 4 November 2026 followed the longer path to the shorter path.
That observation does not explain or invalidate the reviewer's experience; the
cause of the discrepancy has not been established. The historical trace above is
based on the saved response and extraction artifacts, not an assumption that the
URLs are permanently equivalent or that current content is unchanged.

The ORKA example remains a demonstration of following a product mention to its
creator. The screenshot alone does not establish Folkly's AI functionality.

## Knowledge graph extension — 20 November 2026

The University of Toronto–Geoffrey Hinton graphic is from a **different, newer
research run**, not the original report export. Its eight assertions were retrieved
from the live Neo4j graph and traced to one saved extraction chunk from
[Layer 6's website](https://layer6.ai/). The extraction artifact's SHA-256 matched
the graph lineage, and every included quotation was present in the saved source
chunk. See [knowledge-graph-evidence.json](knowledge-graph-evidence.json) for the
public statements, quotations, hash and capture description. No source was fetched
again and no model extraction was rerun to prepare this example.

The image is a screenshot of the application's graph canvas. For this editorial
excerpt, unrelated nodes were hidden and node positions and label sizes were
adjusted in the browser for readability. All eight assertions and their endpoints
were already in the application's live graph response. No relationships, node
labels or source records were changed, and no duplicate names were merged. The
screenshot excludes local session identifiers and private interface details.

The chain uses the source-scoped label **Maks**, exactly as extracted; it is not a
claim of cross-document identity resolution. The graph records what the source
asserts and retains its wording, including temporal limitations. This is a visual
route through several assertions, not proof of a new relationship between the
endpoints. The original report, metrics and report provenance remain unchanged.
