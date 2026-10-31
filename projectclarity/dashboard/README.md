# Dashboard and knowledge explorer

The local HTTP server serves saved research and starts workers only through explicit
research actions. Viewing the graph does not start research or call a model.
Run `python -m projectclarity.dashboard` from the repository root; see the
[root README](../../README.md) for installation and user-facing controls.

## Reading order and ownership

1. `server.py`: loopback HTTP routing, host/CSRF checks and worker actions.
   `/graph?run=RUN_ID` serves the explorer; `/api/runs/RUN_ID/graph` supplies data.
2. `graph.py`: validates the run, reads extraction receipts, rechecks evidence and
   includes that run's SQLite sightings. It includes discoveries outside the report.
3. `graph_grouping.py`: pure, reversible display grouping, not database identity
   resolution. Exact organization names and quoted aliases group; explicit
   person/source/claim links attach structured records. Complete matching records
   compose those groups. Names alone never merge people across sources.
4. `static/graph.js`: selection, inspector, filters, expansion/history and bundled
   Cytoscape layouts. `graph.html`/`graph.css` supply controls and presentation.
5. `projection.py`, `static/app.js` and `static/discoveries.js`: the main run view,
   distinct from the all-discoveries graph projection.

## Important boundaries

- An extracted relationship and a structured field association are distinct edge
  categories. Layout hierarchy is connection distance, not inferred reporting rank.
- Group members retain original fields, sources and review statuses. Grouped status
  summaries do not promote invalid or unreviewed observations to supported facts.
- Node counts include records, field values and statements; they are not unique
  people counts. The legacy `grouped_observations` ledger field counts original
  node representations inside groups, not evidence observations.
- The graph endpoint accepts `group=none` for raw representations. Its default
  `group=organizations` is a legacy name for all supported display grouping rules,
  including person-record attachments and complete-record groups.
- Search and view changes are local. Back stores up to 30 in-memory snapshots of
  scope, filters, positions and viewport. Refresh or grouping changes clear history.
- Relationship filtering uses exact category/label pairs. No LLM or synonym mapping
  silently combines different predicates. Browser assets are local; Cytoscape's
  version, SHA-256 and MIT license are in `static/vendor/`.

## Verification

From the repository root, with dependencies installed:

```bash
.venv/bin/python -m unittest tests.dashboard.test_graph tests.dashboard.test_graph_grouping -v
.venv/bin/python -m evaluation.regression_suite
```

Node.js is required for the real headless Cytoscape checks in `graph_checks.js`.
The Python tests exercise saved-run projection, HTTP routing, SQLite run isolation
and source-integrity behavior using synthetic provider replies. They do not prove
live extraction accuracy. A browser smoke check should also cover search-to-click,
focus/expand/collapse/Back, exact relationship filters, grouping details, responsive
controls, and restoring the raw graph. Playwright is not an application dependency.
