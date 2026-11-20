# Optional local user service

[projectclarity.service](projectclarity.service) is an example systemd user unit,
not a public-hosting configuration. It assumes the checkout is at
`~/ProjectClarity`, with its virtual environment installed and a private `.env`.
Edit the working directory and executable/credential paths if your checkout differs.
`%h` expands to the user's home directory; no developer-specific path is required.

After checking the paths, install from the project root:

```bash
mkdir -p ~/.config/systemd/user
cp deployment/projectclarity.service ~/.config/systemd/user/projectclarity.service
systemctl --user daemon-reload
systemctl --user enable --now projectclarity.service
```

The interface remains at http://127.0.0.1:8775/. Inspect status with
`systemctl --user status projectclarity.service`. Stop active research in the UI
before restarting the service; interrupted work is not automatically replayed.
The unit uses a restrictive file-creation mask, but it does not add authentication
or a public reverse proxy. Keep credentials and saved research outside Git.

## Optional Neo4j service and persistence

The application unit starts Clarity only; it does **not** install or start Neo4j.
For knowledge graph exploration, provision a separate persistent Neo4j service and
follow the [graph configuration guide](../docs/KNOWLEDGE_GRAPH.md). Keep its listeners
local and its credentials in the private environment file or process environment,
never browser assets or version control. Do not overwrite existing model credentials
when adding the database settings.

Retain both the private application data directory and the database's data directory.
Application files contain original evidence, reviewed facts, collection metadata and
report/projection receipts. Neo4j holds a derived graph, not the only copy of knowledge.
A configured but unavailable database affects graph preparation/exploration, not
ordinary research or report generation. Once restored, explicit refresh can rebuild
from saved evidence without new research calls.

Restart Clarity only when research is idle. Interrupted graph jobs are marked as
such rather than silently replayed. Rebuilding an older session can restore its
previously deleted graph records, so graph-only deletion is not deletion of source
research. Collections are logical scopes in this local application, not tenant
security boundaries.
