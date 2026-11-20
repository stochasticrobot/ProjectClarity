"""Store and retrieve collection-scoped evidence using Neo4j and parameterized Cypher.

Saved research files remain the evidence of record. Neo4j holds a rebuildable view:
source mentions connect through assertion nodes that retain full text and quotes.
Queries are fixed application queries, not unrestricted browser-supplied Cypher.
This keeps collection boundaries enforceable in the current single-user app.
"""
import os

from dotenv import dotenv_values


GRAPH_QUERY = '''MATCH (a:ClarityAssertion {collection_id: $collection_id})
WHERE ($session_id = '' OR a.session_id = $session_id)
  AND ($search = '' OR toLower(a.statement) CONTAINS toLower($search))
WITH a ORDER BY a.id LIMIT $limit
MATCH (s:ClarityMention {collection_id: $collection_id})-[:SUBJECT_OF]->(a)
OPTIONAL MATCH (a)-[:OBJECT]->(o:ClarityMention {collection_id: $collection_id})
RETURN properties(s) AS subject, properties(a) AS assertion, properties(o) AS object'''


class Neo4jStore:
    """Own the driver connection; never expose its configuration through the API."""

    def __init__(self, env_file):
        """Read private configuration without requiring Neo4j for ordinary research.

        Args:
            env_file: Existing private environment file; process values take priority.
        Returns:
            None. Creates a lazy driver only when URI and password are configured.
            Missing configuration leaves the graph unavailable, not silently in memory.
        """
        config = {**dotenv_values(env_file), **os.environ}
        self.driver = None
        self.database = config.get('NEO4J_DATABASE', 'neo4j')
        if config.get('NEO4J_URI') and config.get('NEO4J_PASSWORD'):
            from neo4j import GraphDatabase
            self.driver = GraphDatabase.driver(
                config['NEO4J_URI'], auth=(config.get('NEO4J_USERNAME', 'neo4j'), config['NEO4J_PASSWORD']),
                connection_timeout=5, max_transaction_retry_time=5)

    def query(self, cypher, parameters):
        """Execute a bounded application query with values separate from Cypher text.

        Args:
            cypher: Trusted application-owned Cypher, never browser input.
            parameters: Values bound to that query, including collection identity.
        Returns:
            List of plain result dictionaries. Missing configuration or database
            failure raises a generic error without connection details or credentials.
        """
        if self.driver is None:
            raise RuntimeError('Neo4j is not configured. See the graph setup guide.')
        try:
            records, _, _ = self.driver.execute_query(cypher, parameters_=parameters, database_=self.database)
            return [record.data() for record in records]
        except Exception:
            raise RuntimeError('Neo4j is unavailable; saved research evidence is unchanged.') from None

    def replace_session(self, collection_id, session_id, entities, assertions):
        """Atomically replace one session projection so retries cannot leave stale edges.

        Args:
            collection_id: Owning collection UUID supplied by the session service.
            session_id: Research session UUID, never chosen by an extraction model.
            entities: Validated source-local mentions prepared from saved evidence.
            assertions: Validated assertions with source quotations and lineage.
        Returns:
            Counts of projected mentions and assertions. The whole transaction rolls
            back on failure. Other sessions and collections are never deleted.
        """
        if self.driver is None:
            raise RuntimeError('Neo4j is not configured. See the graph setup guide.')
        for label in ('ClarityMention', 'ClarityAssertion'):
            self.query(f'CREATE CONSTRAINT {label.lower()}_identity IF NOT EXISTS '
                       f'FOR (n:{label}) REQUIRE (n.collection_id, n.id) IS UNIQUE', {})
        try:
            with self.driver.session(database=self.database) as session:
                with session.begin_transaction(timeout=30) as tx:
                    params = {'collection_id': collection_id, 'session_id': session_id}
                    tx.run('MATCH (n) WHERE (n:ClarityMention OR n:ClarityAssertion) '
                           'AND n.collection_id = $collection_id AND n.session_id = $session_id '
                           'DETACH DELETE n', **params).consume()
                    tx.run('UNWIND $rows AS row CREATE (n:ClarityMention) SET n = row, '
                           'n.collection_id = $collection_id', rows=entities, **params).consume()
                    tx.run('UNWIND $rows AS row CREATE (a:ClarityAssertion) SET a = row, '
                           'a.collection_id = $collection_id WITH a, row '
                           'MATCH (s:ClarityMention {collection_id: $collection_id, id: row.subject}) '
                           'CREATE (s)-[:SUBJECT_OF]->(a) WITH a, row '
                           'OPTIONAL MATCH (o:ClarityMention {collection_id: $collection_id, id: row.object}) '
                           'FOREACH (_ IN CASE WHEN o IS NULL THEN [] ELSE [1] END | CREATE (a)-[:OBJECT]->(o))',
                           rows=assertions, **params).consume()
                    tx.commit()
        except Exception:
            raise RuntimeError('Graph update failed; the previous projection and saved evidence are retained.') from None
        return {'mentions': len(entities), 'assertions': len(assertions)}

    def explore(self, collection_id, search='', session_id='', limit=250):
        """Return an explicitly bounded evidence graph and the Cypher that produced it.

        Args:
            collection_id: Selected collection UUID.
            search: Case-insensitive substring of complete assertion statements.
            session_id: Optional research-session filter; empty includes all sessions.
            limit: Maximum assertions displayed, between one and five hundred.
        Returns:
            Graph rows, executed Cypher and public parameters. One extra row detects
            truncation; no completeness claim is made for a limited view.
        """
        parameters = dict(collection_id=collection_id, search=search, session_id=session_id, limit=limit + 1)
        rows = self.query(GRAPH_QUERY, parameters)
        return {'rows': rows[:limit], 'has_more': len(rows) > limit,
                'cypher': GRAPH_QUERY, 'parameters': parameters}

    def close(self):
        """Release database connections during application shutdown.

        Args: None.
        Returns: None; does nothing when the graph was not configured.
        """
        if self.driver is not None:
            self.driver.close()
