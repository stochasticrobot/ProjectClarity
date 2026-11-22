# Run the evidence graph

ProjectClarity uses Neo4j Community Edition and the official Python driver for
persistence and Cypher queries. Cytoscape.js renders results locally in the browser;
no CDN requests or database credentials are sent to the browser.

Start at the ordinary question form (`/`). Research and report generation proceed
normally. At the final research boundary, an independent worker also prepares the
Neo4j graph. Open **Knowledge graph** from any research/report screen to inspect
available evidence; there is no manual report-generation gate. A slow or failed
Neo4j update cannot stop reporting, and graph changes cannot alter report inputs.
The report remains the primary flow. A quiet **Knowledge graph ready — Explore**
notice appears when graph preparation completes, even if the report finishes first.
Only clicking a graph link opens the explorer; there is no automatic graph navigation
or panel opening. An explorer already opened by the user can refresh when ready.

The explorer uses a compact search panel, a graph with its assertion list immediately
below, and a right-hand details panel on wider screens. On mobile, the panels stack
and selecting an assertion brings its details into view. Contributing research and
Cypher details are collapsed below the main workspace. Searches show up to 250
assertions by default (adjustable to 500). Selecting a list assertion highlights
and centers its graph diamond and highlights its connecting edges. The graph
provides zoom-in and zoom-out controls without changing the search results.

New research gets a fresh knowledge session automatically. In expanded Settings,
choose an existing knowledge-session ID to accumulate evidence deliberately.
Internally this boundary is `collection_id`; the research `session_id` remains
a separate lineage identifier. The assigned knowledge ID appears after setup.
Duplicate names remain separate source mentions, not resolved identities.

## Acceptance contract

Two ordinary research starts must produce different persisted graph collections
through production HTTP, even for identical questions and entity names. An explicit
existing selection must accumulate in only that collection. Duplicate submissions
must retain their assigned ID; invalid IDs fail instead of falling back.
Every graph assertion retains its source quote and lineage; repeated projection
must not duplicate records, and saved data must survive reopening the application.

Independence is tested in both directions: hold the graph worker and observe the
report complete; hold report I/O and query the completed graph. A graph failure
must leave report completion intact. Refresh must work during reporting without
starting extra model calls or rewriting report inputs. The report uses reviewed
facts on disk, never the graph database.

This does not claim identity resolution, independent fact verification,
multi-user authorization, arbitrary-query isolation or cross-run AI reasoning.

## Install and configure

Install the application as described in the [main README](../README.md). Set up a
local Neo4j Community server using the [official installation guide](https://neo4j.com/docs/operations-manual/current/installation/).
The validated development versions are Neo4j Community 2026.10.2, Java 21 and the
Neo4j Python driver pinned in `requirements.txt`. Neo4j needs its own persistent
data directory outside the repository; Python installation alone does not install
or start a database.

Keep Bolt and HTTP listeners on loopback for this local application. Enable database
authentication, choose a unique password, and store connection values only in your
ignored `.env` file or process environment:

```dotenv
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=replace-with-your-local-password
NEO4J_DATABASE=neo4j
```

The account must be able to create the two uniqueness constraints, write the
projection, and query it. The application never returns this configuration through
its graph API. Existing provider credentials should be preserved when adding it.
Restart an idle application after changing configuration. Without configuration,
ordinary research remains available and the explorer reports that Neo4j is absent.

For local-only operation, review these Neo4j 2026.10 settings in `neo4j.conf`:

```properties
server.default_listen_address=127.0.0.1
dbms.usage_report.enabled=false
dbms.fleet_manager.enabled=false
server.fleet_discovery.enabled=false
server.bolt.telemetry.enabled=false
```

See the [official configuration reference](https://neo4j.com/docs/operations-manual/current/configuration/configuration-settings/).
Do not commit database files, connection files, logs, private source documents,
research output or screenshots of private evidence. The normal application data
location is ignored `runs/`; external database data needs its own private location.

## Validate

The normal suite checks input handling without requiring a database:

```bash
.venv/bin/python -m unittest tests.integration.test_knowledge_graph -v
```

To run the real-Neo4j production-entry test, provide the path to a private connection
file (same variable names as above):

```bash
CLARITY_TEST_NEO4J_ENV=/path/to/private-test.env \
  .venv/bin/python -m unittest tests.integration.test_knowledge_graph -v
```

Use a development database. The test creates uniquely identified fixture collections
and leaves their graph records for inspection; it never clears an existing database.
It runs controlled questions through the actual HTTP workflow, replacing only external
search, document HTTP and model replies. Neo4j writes and Cypher reads are real.
This proves routing and persistence contracts, not live model extraction quality.

The browser library is vendored at version 3.33.1 with its MIT license in
`src/projectclarity/ux/scripts/cytoscape.LICENSE`.

See [collection behavior and limitations](../src/projectclarity/knowledge_graph/README.md).

## Fresh-session verification (2026-11-22)

The full regression suite ran 220 tests: 216 passed, 4 database-dependent tests
skipped. The graph suite separately ran against real Neo4j: all 6 tests passed.
Three fixture-driven production HTTP runs demonstrated two fresh isolated graphs
and explicit third-run reuse of the first. Final Cypher results contained exactly
the intended research-session assertions; the historical default stayed empty.
Other checks covered duplicate start idempotency, unknown/reassigned IDs,
persistence, refresh and graph/report independence. Models/search/downloads were
fixtures for these checks; this is not a new live Bedrock research benchmark.

Headless browser checks exercised expanded Settings, full existing IDs, explicit
submission, displayed assigned ID, new-question reset, fresh submission, narrow
layout and unavailable-list handling. The local idle service was then restarted;
its effective configuration resolved to Bedrock Nova Micro in us-east-2, and its
served page rendered the fresh default and existing dropdown choices. No user
research was submitted. An actual user's browser rendering remains unobserved.

During regression, older Anthropic-mocked CLI/budget fixtures inherited the real
Bedrock `.env` and failed. Those runs are not passing evidence. Fixtures now pin
their intended provider/model and forbid Bedrock client construction; the final
suite above ran with the actual Bedrock `.env` still in place.
