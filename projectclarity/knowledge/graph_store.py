"""Store broad source observations in SQLite and expose newly committed records.

This knowledge graph is a collection of entities, relationships and attributes,
not the LangGraph workflow that executes research. For example, a captured person,
organization and affiliation can be stored with the source version and quotation
that supplied them. Stable source/name/type keys avoid duplicating the same
observation on replay, without guessing that equal names on different sources
identify the same entity.

Writes and their change-feed events commit together so other processes can inspect
new records. Contradictory or changed source versions can coexist. These are
source-linked capture observations, not reviewed answer facts. SQLite stores the
records locally; no separate graph database server is required.
"""
import hashlib
import json
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_meta(version INTEGER NOT NULL);
INSERT INTO graph_meta SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM graph_meta);
CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mentions(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assertions(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sightings(run_id TEXT, record_id TEXT, PRIMARY KEY(run_id, record_id));
CREATE TABLE IF NOT EXISTS changes(seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL, kind TEXT NOT NULL, record_id TEXT NOT NULL, payload TEXT NOT NULL);
"""


def stable_id(kind, *parts):
    """Derive a repeatable record ID so replaying identical observations is idempotent.

    Args:
        kind: Application-owned prefix identifying the record category.
        *parts: JSON-serializable identity components; provenance belongs in assertion IDs.

    Returns:
        A category-prefixed SHA-256 identifier; no model-selected database key is trusted.
    """
    encoded = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return kind + "_" + hashlib.sha256(encoded.encode()).hexdigest()


def normalized_name(value):
    """Normalize spelling layout for same-source reuse without guessing cross-source identity.

    Args:
        value: Entity label explicitly found in source text.

    Returns:
        NFC, case-folded text with collapsed whitespace; accents and punctuation remain.
    """
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


@contextmanager
def connect_store(path, *, create=False):
    """Open a bounded-lifetime store connection so graph writes and feed events commit together.

    Args:
        path: Local knowledge database Path, separate from workflow checkpoint storage.
        create: Whether to initialize the store; False opens an existing database read-only.

    Returns:
        A context manager yielding a sqlite3 connection; commit writes on success, roll back
        on failure and always close. Missing/read-only stores are never silently created.
    """
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Knowledge database must not be a symlink")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10)
        path.chmod(0o600)
    else:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        connection.execute("BEGIN")  # Snapshot collections and cursor share one read version.
    connection.row_factory = sqlite3.Row
    try:
        if create:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
        if connection.execute("SELECT version FROM graph_meta").fetchone()[0] != 1:
            raise ValueError("Unsupported knowledge schema version")
        with connection:
            yield connection
    finally:
        connection.close()


def insert_record(connection, table, record, run_id):
    """Insert a graph record once and announce new records or new-run sightings atomically.

    Args:
        connection: Open writable store connection inside the batch transaction.
        table: One of the application-owned graph table names, never model-supplied SQL.
        record: JSON-compatible record including its stable id.
        run_id: Research run that observed the record.

    Returns:
        True if this record is newly inserted; False for a repeat. A new-run sighting still
        creates a change-feed event so clients can show corroborating observations.
    """
    if table not in {"nodes", "sources", "mentions", "assertions"}:
        raise ValueError("Unknown graph table")
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
    added = connection.execute(f"INSERT OR IGNORE INTO {table}(id,payload) VALUES(?,?)", (record["id"], payload)).rowcount == 1
    seen = connection.execute("INSERT OR IGNORE INTO sightings VALUES(?,?)", (run_id, record["id"])).rowcount == 1
    if added or seen:
        event = record if added else {"id": record["id"], "record_kind": table}
        connection.execute("INSERT INTO changes(run_id,kind,record_id,payload) VALUES(?,?,?,?)",
            (run_id, table if added else "sighting", record["id"], json.dumps(event, ensure_ascii=False)))
    return added


def commit_observations(path, run_id, source, entities, assertions):
    """Commit one validated capture batch so other processes can see graph growth immediately.

    Args:
        path: Local graph-database Path shared by runs using the same output root.
        run_id: Research run contributing this batch.
        source: Source location, text hash and retrieval timestamp for this exact version.
        entities: Validated mentions with local entity_id, name, kind and anchored evidence.
        assertions: Validated atomic attributes/relationships referencing entities in this batch.

    Returns:
        Counts of newly inserted nodes, mentions and assertions plus cursor_start (exclusive)
        and cursor (inclusive) delimiting this batch's feed records. Consumers must also
        filter by run_id to exclude concurrent runs. Return only after commit succeeds.
        Same names from different sources remain separate unresolved entities.
    """
    version = {**source, "id": stable_id("source", source["location"], source["text_sha256"])}
    mapping = {}
    counts = {"new_nodes": 0, "new_mentions": 0, "new_assertions": 0}
    with connect_store(path, create=True) as connection:
        counts["cursor_start"] = connection.execute("SELECT COALESCE(MAX(seq),0) FROM changes").fetchone()[0]
        insert_record(connection, "sources", version, run_id)
        for entity in entities:
            node_id = stable_id("entity", source["location"], entity["kind"], normalized_name(entity["name"]))
            mapping[entity["entity_id"]] = node_id
            node = {"id": node_id, "name": entity["name"], "kind": entity["kind"],
                "identity_scope": source["location"], "identity_status": "source_scoped_unresolved"}
            counts["new_nodes"] += insert_record(connection, "nodes", node, run_id)
            mention = {"id": stable_id("mention", node_id, version["id"], entity["evidence"]),
                "node_id": node_id, "source_version": version["id"], "evidence": entity["evidence"],
                "status": "source_anchored_unreviewed"}
            counts["new_mentions"] += insert_record(connection, "mentions", mention, run_id)
        for assertion in assertions:
            record = {"subject": mapping[assertion["subject_id"]], "predicate": assertion["predicate"],
                "object": mapping[assertion["object_id"]] if assertion["object_id"] else None,
                "value": assertion["value"], "role": assertion["role"],
                "source_version": version["id"], "evidence": assertion["evidence"],
                "status": "source_anchored_unreviewed", "answer_relevance": "unassessed"}
            record["id"] = stable_id("assertion", record)
            counts["new_assertions"] += insert_record(connection, "assertions", record, run_id)
        counts["cursor"] = connection.execute("SELECT COALESCE(MAX(seq),0) FROM changes").fetchone()[0]
    return counts


def read_committed_additions(path, run_id, since, through):
    """Read one capture batch's new graph records for private, post-commit console display.

    Args:
        path: Existing local knowledge database Path; opened read-only, never created.
        run_id: Owning research run; exclude changes contributed by concurrent runs.
        since: Exclusive feed cursor recorded before this capture batch's writes.
        through: Inclusive cursor recorded at the end of that same committed batch.

    Returns:
        New nodes, relationships and attributes in insertion order within each category.
        Assertions include subject_name/object_name resolved from persisted nodes, including
        older endpoints. Mentions and repeat sightings are omitted, so replay is not new
        knowledge. Invalid bounds, missing endpoints or unreadable stores raise an error;
        this reader never changes records or makes a model call.
    """
    if not 0 <= since <= through:
        raise ValueError("Invalid committed change bounds")
    result = {"nodes": [], "relationships": [], "attributes": []}
    with connect_store(path) as connection:
        rows = connection.execute(
            "SELECT kind,payload FROM changes WHERE seq>? AND seq<=? AND run_id=? "
            "AND kind IN ('nodes','assertions') ORDER BY seq", (since, through, run_id)).fetchall()
        for row in rows:
            record = json.loads(row["payload"])
            if row["kind"] == "nodes":
                result["nodes"].append(record)
                continue
            for endpoint in ("subject", "object"):
                if record[endpoint] is None:
                    continue
                node = connection.execute("SELECT payload FROM nodes WHERE id=?", (record[endpoint],)).fetchone()
                if node is None:
                    raise ValueError("Committed assertion has a missing endpoint")
                record[endpoint + "_name"] = json.loads(node[0])["name"]
            kind = "relationships" if record["object"] is not None else "attributes"
            result[kind].append(record)
    return result


def read_knowledge(path, *, since=None, limit=1000):
    """Read a graph snapshot or ordered change page without invoking research or a model.

    Args:
        path: Existing local knowledge database Path.
        since: Exclusive change cursor; None requests a bounded snapshot instead of events.
        limit: Maximum records per collection or change page, between 1 and 10000.

    Returns:
        JSON-compatible snapshot or events with counts/truncation/cursor metadata. A snapshot
        can be partial; clients must not treat omitted endpoints as deleted graph entities.
    """
    if not 1 <= limit <= 10000 or (since is not None and since < 0):
        raise ValueError("Invalid graph page bounds")
    with connect_store(path) as connection:
        if since is not None:
            rows = connection.execute("SELECT * FROM changes WHERE seq>? ORDER BY seq LIMIT ?", (since, limit + 1)).fetchall()
            events = [{**dict(row), "payload": json.loads(row["payload"])} for row in rows[:limit]]
            return {"events": events, "cursor": events[-1]["seq"] if events else since, "has_more": len(rows) > limit}
        result = {"schema_version": 1, "identity_policy": "source_scoped; name equality is not identity",
            "status": "source_anchored_observations_not_accepted_facts", "counts": {}, "truncated": {}}
        for table in ("nodes", "sources", "mentions", "assertions"):
            result["counts"][table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            result[table] = [json.loads(row[0]) for row in connection.execute(f"SELECT payload FROM {table} ORDER BY id LIMIT ?", (limit,))]
            result["truncated"][table] = result["counts"][table] > len(result[table])
        result["cursor"] = connection.execute("SELECT COALESCE(MAX(seq),0) FROM changes").fetchone()[0]
        return result
