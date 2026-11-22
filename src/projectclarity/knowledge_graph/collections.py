"""Remember which research sessions belong together without mixing their evidence.

Collection names and membership live beside private session files. Neo4j stores
only the evidence projection. Creating a collection therefore remains possible
when the database is offline, and a failed graph update never loses research.
"""
import json
import os
from threading import RLock
from uuid import uuid4

from fastapi import HTTPException
from projectclarity.knowledge_graph.evidence_projection import evidence_rows
from projectclarity.knowledge_graph.neo4j_store import Neo4jStore


class Collections:
    """Manage collection metadata and serialize projection updates in this process."""

    def __init__(self, root, env_file):
        """Connect private metadata and the optional Neo4j projection.

        Args:
            root: Existing application session directory.
            env_file: Private provider/database configuration file.
        Returns: None; opens no database connection until a graph operation occurs.
        """
        self.root = root
        self.path = root / 'collections.json'
        self.lock = RLock()
        self.store = Neo4jStore(env_file)

    def list(self):
        """Read saved collection choices without querying research or model providers.

        Args: None.
        Returns: Collection dictionaries, or an empty list before the first creation.
            Corrupt metadata raises rather than silently replacing membership.
        """
        with self.lock:
            return json.loads(self.path.read_text()) if self.path.exists() else []

    def require(self, collection_id):
        """Reject unknown collection identities before any read or write to Neo4j.

        Args: collection_id: Collection UUID string from validated application input.
        Returns: Saved collection metadata; raises HTTP 404 when absent.
        """
        found = next((row for row in self.list() if row['id'] == collection_id), None)
        if found is None:
            raise HTTPException(404, 'Collection not found.')
        return found

    def create(self, name, *, is_default=False):
        """Create a distinct collection even when another one has the same display name.

        Args:
            name: Display label, between one and 120 non-whitespace characters.
            is_default: Internal marker for the shared default; ordinary creation
                leaves it false. Call default() to reuse an existing default.
        Returns: New collection metadata. Saves owner-only JSON atomically; invalid
            names raise HTTP 422. Does not start research or create inferred identities.
        """
        name = name.strip()
        if not name or len(name) > 120:
            raise HTTPException(422, 'Enter a collection name of 1–120 characters.')
        with self.lock:
            rows = self.list()
            row = {'id': str(uuid4()), 'name': name}
            if is_default:
                row['is_default'] = True
            rows.append(row)
            temporary = self.path.with_suffix('.tmp')
            with temporary.open('w') as stream:
                os.chmod(temporary, 0o600)
                json.dump(rows, stream, ensure_ascii=False, indent=2)
            temporary.replace(self.path)
            return row

    def default(self):
        """Preserve the historical shared collection for legacy graph routes.

        Args: None.
        Returns: Persisted default metadata. Creates it atomically on first use;
            new research does not use this collection unless explicitly selected.
            Other collections are never renamed, merged or selected by their name.
            The reentrant lock prevents duplicate defaults in this application.
        """
        with self.lock:
            found = next((row for row in self.list() if row.get('is_default')), None)
            return found if found is not None else self.create('Default', is_default=True)

    def sync(self, state):
        """Project accepted evidence from one inactive session into its own collection.

        Args: state: Authoritative session snapshot with immutable collection membership.
        Returns: Projected record counts; missing membership raises HTTP 409. Invalid
            evidence or unavailable Neo4j raises without modifying research files.
            No external research, model calls or name-based identity merging occurs.
        """
        collection_id = state.get('collection_id')
        if not collection_id:
            raise HTTPException(409, 'This older session has no collection membership.')
        self.require(collection_id)
        with self.lock:
            entities, assertions = evidence_rows(self.root / state['session_id'], state)
            return self.store.replace_session(collection_id, state['session_id'], entities, assertions)
