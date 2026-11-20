"""Connect collection choices and evidence exploration to the local web interface.

The browser supplies filters, not arbitrary Cypher. The server chooses the saved
collection boundary and returns the exact query for inspection. No graph read can
start research or refresh the database as a hidden side effect.
"""
from uuid import UUID

from fastapi import HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from projectclarity.model_access.model_client import ROOT


class CollectionInput(BaseModel):
    """Accept a display name without accepting caller-controlled collection IDs."""
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=120)


def register_graph_routes(app, sessions):
    """Attach collection and graph endpoints to the existing protected local app.

    Args:
        app: FastAPI instance with the same-origin and loopback middleware.
        sessions: Owning session service and its collection repository.
    Returns: None; registers routes without opening Neo4j or making model calls.
    """
    @app.get('/graph')
    @app.get('/collections')
    def page():
        """Serve the default evidence graph without exposing collection management.

        Args: None.
        Returns: Packaged HTML; no database write or research is triggered.
        """
        return FileResponse(ROOT / 'ux/pages/knowledge_graph.html')

    @app.get('/api/collections')
    def collections():
        """List the current local user's collection choices.

        Args: None.
        Returns: Public labels and IDs; no credentials or filesystem paths.
        """
        return sessions.collections.list()

    @app.post('/api/collections', status_code=201)
    def create(data: CollectionInput):
        """Create an empty collection without starting research.

        Args: data: Validated display name.
        Returns: New collection ID and name, or validation error for blank text.
        """
        return sessions.collections.create(data.name)

    @app.get('/api/collections/{cid}/sessions')
    def collection_sessions(cid: UUID):
        """List research contributing to one collection, including projection failures.

        Args: cid: Existing collection UUID.
        Returns: Session IDs, questions and graph status; no raw receipts or private paths.
        """
        sessions.collections.require(str(cid))
        rows = []
        for path in sorted(sessions.root.glob('*/session.json')):
            state = sessions.read(path.parent.name)
            if state.get('collection_id') == str(cid):
                rows.append({'session_id': state['session_id'], 'question': state['original_question'],
                             'status': state['status'], 'graph': state.get('knowledge_graph', {'status': 'not_projected'})})
        return rows

    @app.get('/api/collections/{cid}/graph')
    def graph(cid: UUID, search: str = Query('', max_length=200),
              session_id: str = Query('', max_length=36), limit: int = Query(250, ge=1, le=500)):
        """Query one collection's evidence using bounded, parameterized Cypher.

        Args:
            cid: Existing collection UUID.
            search: Optional statement substring.
            session_id: Optional saved session filter; cannot broaden collection scope.
            limit: Maximum displayed assertions, between one and five hundred.
        Returns: Source-linked graph rows and executed query; HTTP 503 on database
            failure. Error text never includes raw driver exceptions or credentials.
        """
        sessions.collections.require(str(cid))
        try:
            return sessions.collections.store.explore(str(cid), search, session_id, limit)
        except RuntimeError as error:
            raise HTTPException(503, str(error)) from None

    @app.get('/api/knowledge-graph')
    def default_graph(search: str = Query('', max_length=200),
                      limit: int = Query(250, ge=1, le=500)):
        """Explore the shared default without requiring a collection choice.

        Args:
            search: Optional substring of assertion statements.
            limit: Maximum displayed assertions, from one to five hundred.
        Returns: The same scoped Cypher result as the internal collection endpoint;
            unavailable Neo4j returns HTTP 503. Does not import historical evidence.
        """
        return graph(UUID(sessions.default_collection['id']), search, '', limit)

    @app.get('/api/knowledge-graph/sessions')
    def default_sessions():
        """List only sessions assigned to the shared default graph.

        Args: None.
        Returns: Public research questions and projection status; other collections
            and unassigned historical sessions stay separate.
        """
        return collection_sessions(UUID(sessions.default_collection['id']))

    @app.post('/api/setup/{sid}/graph-sync')
    def sync(sid: UUID):
        """Explicitly refresh one inactive session's saved evidence projection.

        Args: sid: Saved research UUID; collection membership comes from the server.
        Returns: Counts or sanitized failure status; active research produces HTTP 409.
        """
        return sessions.sync_graph(str(sid))

