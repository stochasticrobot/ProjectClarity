# Knowledge graph

This folder builds ProjectClarity's knowledge graph from the entities and assertions
extracted during research. It stores them in **Neo4j** and uses **Cypher** to retrieve
the connections shown in the knowledge graph explorer. Each assertion retains its
source quotation and research lineage, so users can inspect the evidence behind a
relationship rather than seeing an unexplained connection between two names.

The graph is an **explorable view of evidence**, not an additional authority.
Original extraction records remain on disk as the source of truth. Neo4j holds a
derived view that can be rebuilt without repeating searches, downloads or model
extraction. Report generation continues to use saved research records, not Neo4j.

## How the graph represents evidence

The graph stores two kinds of nodes:

- **Entity mentions** (`ClarityMention`): the people, organizations, places and other
  entities identified in a particular source extraction. These appear blue in the
  explorer.
- **Assertions** (`ClarityAssertion`): what a source says about an entity or its
  relationship to another entity. These appear as orange diamonds and retain the
  predicate, full statement, original quote, source URL and extraction lineage.

Relationships connect the subject mention to the assertion and, when present, the
assertion to its object mention:

```text
Subject mention ── SUBJECT_OF ──> Assertion ── OBJECT ──> Object mention
```

An assertion about a literal value does not need an object node; its value and full
statement remain on the assertion. Keeping assertions as nodes lets the explorer
show the evidence for each connection, including uncertainty or negation in the
saved statement, instead of reducing it to an unqualified relationship.

Identical names are **not merged automatically**. Two mentions with the same name
can appear as separate nodes, even within one research session. This package does
not yet resolve identities or aliases.

## From extracted records to an explorable graph

1. **Read saved evidence.** The projection reads the current and retained research
   rounds, using the document pipeline's knowledge indexes to locate accepted
   extraction records. It checks artifact hashes and prepares entities, assertions
   and source references. Quotes are rechecked when the receipt includes source text.
2. **Build source-linked records.** Stable identifiers tie each mention and assertion
   to its research session, extraction artifact and content hash. Assertions retain
   their document, chunk, round and original fact identifiers.
3. **Write to Neo4j.** A transaction replaces one session's graph records while
   preserving other sessions. Rebuilding the same session does not accumulate
   duplicates; a failed transaction leaves its previous graph intact.
4. **Query with Cypher.** Fixed, parameterized queries retrieve assertions and their
   subject/object mentions. Searches match assertion text and can filter by research
   session. The explorer exposes the query for inspection, not arbitrary editing.
5. **Display the evidence.** The web layer renders the returned graph with
   Cytoscape.js. Selecting a displayed assertion highlights its graph node and
   connections and shows its statement, quotation and source details.

The query returns up to **250 assertions by default**, adjustable to 500. Results
are ordered by stable record ID, not relevance. A notice identifies additional
matches beyond the limit; the displayed subset is not a complete graph overview.

## How it fits into research

At the end of research/review, an independent worker prepares the graph from a
frozen snapshot of saved extraction records **alongside automatic report generation**.
Neither task waits for the other, and a Neo4j failure cannot block or change the
report. A fast report may finish before the graph.

The report remains the main flow. A **Knowledge graph ready — Explore** notice tells
the user when the graph is available; the explorer opens only when the user chooses
it. **Refresh saved evidence** rebuilds the graph without rerunning research, including
while reporting is in progress.

Graph progress is stored separately in `knowledge-graph.json`. The graph worker
never writes report inputs or research status, and report retries do not trigger
another graph build. An interrupted graph job is marked interrupted on startup,
not silently retried.

## Files in this folder

| File | Responsibility |
| --- | --- |
| [evidence_projection.py](evidence_projection.py) | Reads accepted saved extractions, checks their hashes, and builds source-linked mention and assertion records. |
| [neo4j_store.py](neo4j_store.py) | Owns the database connection, replaces session graphs transactionally, and retrieves evidence through parameterized Cypher. |
| [projection_jobs.py](projection_jobs.py) | Runs graph preparation independently of reporting and saves graph-only progress. |
| [collections.py](collections.py) | Maintains research-session grouping and coordinates projection into the appropriate graph collection. |

The related [web routes](../ux/graph_routes.py) provide the graph API and explorer
page; the [browser script](../ux/scripts/knowledge_graph.js) renders and interacts
with the returned graph. Database setup belongs in the
[knowledge graph setup guide](../../../docs/KNOWLEDGE_GRAPH.md).

## Grouping evidence across research sessions

New research sessions share one persistent default collection, allowing evidence
from several questions to accumulate. Multiple collections are supported internally
through `collection_id`, but creation and selection are not exposed in the interface.
Separate collections can keep unrelated bodies of evidence apart—for example,
fictional worlds and real-world research.

Reads and writes are scoped to a collection. A session-linked explorer uses that
session's collection; existing memberships are not automatically renamed or merged.
Older sessions without collection membership are not automatically migrated. Moving
sessions between collections and deleting collections are not implemented.

These are logical boundaries in a **single-user local application**, not multi-user
access controls. Neo4j administrator access can see all collections.

## Evidence limits and development

The graph includes accepted extraction assertions, not only facts selected for the
report. Acceptance by extraction checks does **not** mean independent verification.
Later claim-review decisions are not currently overlaid on the graph; the inspector
labels assertions accordingly. The graph does not provide cross-run AI reasoning.

When fixing graph data or validating projection changes, reuse the saved extraction
records and rebuild through the normal projection path. Do not repeat searches,
downloads or extraction just to test a graph change. If existing records cannot
support a needed check, explain the gap and obtain approval before new acquisition.
Keep private research fixtures, database credentials and validation output out of Git.

The [production-entry regression](../../../tests/integration/test_knowledge_graph.py)
checks graph behavior through the application. See the
[validation instructions](../../../docs/KNOWLEDGE_GRAPH.md#validate) for running it
with or without a configured development database.
