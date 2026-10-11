"""FalkorDB connection layer.

Three modes:
- EMBEDDED (default): real FalkorDB engine via falkordblite (redislite).
  No Docker, no server needed. Used for local dev and demo.
- EXTERNAL: connect to a real FalkorDB server (FalkorDB Cloud / Docker)
  when FALKOR_HOST is set. Used at hackathon time.
- MEMORY (fallback): pure-Python in-memory graph when neither works
  (e.g. Vercel serverless blocks the redislite binary). Same data model,
  same API — demo stays fully functional.

Embedded/external run REAL Cypher against a REAL FalkorDB engine.
Memory mode is an honest fallback for serverless (labeled in /api/status).
"""
import os
import traceback

GRAPH_NAME = "dealership_brain"

# Last graph initialization error (for diagnostics via /api/status)
_last_error = None

# Singleton memory fallback (per process)
_memory_graph = None


def get_last_error():
    return _last_error


def _get_memory_graph():
    global _memory_graph
    if _memory_graph is None:
        from app.memory_graph import MemoryGraph
        _memory_graph = MemoryGraph()
    return _memory_graph


def _is_memory(graph):
    from app.memory_graph import MemoryGraph
    return isinstance(graph, MemoryGraph)


def get_db():
    global _last_error
    host = os.environ.get("FALKOR_HOST")
    if host:
        try:
            from falkordb import FalkorDB
            port = int(os.environ.get("FALKOR_PORT", "6379"))
            password = os.environ.get("FALKOR_PASSWORD") or None
            db = FalkorDB(host=host, port=port, password=password)
            _last_error = None
            return db, "external"
        except Exception as e:
            _last_error = f"External FalkorDB failed: {type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}"
            raise RuntimeError(_last_error)
    try:
        from redislite import FalkorDB
        # Persistent file so seed data survives across processes/restarts.
        # On serverless (Vercel) the app dir (/var/task) is read-only — use /tmp.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        default_dir = os.path.join(here, "falkordb-data")
        data_dir = os.environ.get("FALKOR_DATA_DIR", default_dir)
        try:
            os.makedirs(data_dir, exist_ok=True)
        except OSError:
            # Read-only filesystem (e.g. Vercel /var/task) — fall back to /tmp
            data_dir = os.path.join("/tmp", "falkordb-data")
            os.makedirs(data_dir, exist_ok=True)
        db = FalkorDB(os.path.join(data_dir, "brain.db"))
        _last_error = None
        return db, "embedded"
    except Exception as e:
        # Embedded FalkorDB failed (e.g. redislite binary blocked on serverless).
        # Fall back to pure-Python in-memory graph — demo stays fully functional.
        _last_error = (f"Embedded FalkorDB (redislite) failed: {type(e).__name__}: {e}. "
                       f"Fell back to in-memory graph.")
        return _get_memory_graph(), "memory"


_seeded_this_process = False


def get_graph(auto_seed=True):
    """Return (graph, mode). ensure_schema runs lazily (idempotent) so the app
    works even when the FastAPI startup event never fires (e.g. Mangum on Vercel).

    When auto_seed is True (default), an empty graph is seeded once per process
    with the fictional demo dataset — this makes cold starts on serverless
    (Vercel) immediately demoable. Pass auto_seed=False from seed() itself to
    avoid re-entry.

    Modes: 'external' | 'embedded' | 'memory' (fallback, same API).
    """
    global _seeded_this_process
    try:
        db, mode = get_db()
    except RuntimeError:
        # External FalkorDB explicitly configured but failed — fall back to memory
        # rather than crash (keeps demo alive; /api/status shows the diagnostic).
        return _get_memory_graph(), "memory"
    except Exception:
        return _get_memory_graph(), "memory"
    if mode == "memory":
        # get_db already returned the memory graph directly
        return db, mode
    graph = db.select_graph(GRAPH_NAME)
    # Lazy schema ensure: idempotent, and guarantees indexes exist even when
    # the FastAPI startup event never runs (e.g. Mangum/lifespan="off" on Vercel).
    try:
        ensure_schema(graph)
    except Exception:
        pass
    if auto_seed and not _seeded_this_process:
        _seeded_this_process = True
        try:
            res = graph.query("MATCH (n) RETURN count(n) AS c")
            count = res.result_set[0][0] if res.result_set else 0
            if count == 0:
                from app import seed as seedmod  # lazy: avoids circular import
                seedmod.seed()
        except Exception:
            pass
    return graph, mode


def wipe_graph(graph):
    """Delete everything (fresh demo reset)."""
    if _is_memory(graph):
        # Re-seed a fresh memory graph
        from app.memory_graph import MemoryGraph
        graph._nodes.clear()
        graph._rels.clear()
        graph._next_id = 1
        graph._seed()
        return
    graph.query("MATCH (n) DETACH DELETE n")


SCHEMA_CYPHER = """
// Indexes for fast lookup (idempotent-ish; FalkorDB ignores duplicates gracefully)
CREATE INDEX FOR (c:Customer) ON (c.phone);
CREATE INDEX FOR (v:Vehicle) ON (v.stock_no);
"""


def ensure_schema(graph):
    if _is_memory(graph):
        return  # no indexes needed
    for stmt in [s.strip() for s in SCHEMA_CYPHER.strip().split(";") if s.strip()]:
        try:
            graph.query(stmt)
        except Exception:
            pass  # index may already exist


# ---------------------------------------------------------------------------
# Read helpers used by agents (real Cypher on FalkorDB, native on memory mode)
# ---------------------------------------------------------------------------

def find_customer_by_phone(graph, phone):
    if _is_memory(graph):
        return graph.find_customer_by_phone(phone)
    res = graph.query(
        "MATCH (c:Customer {phone: $phone}) RETURN c.name, c.budget, c.segment",
        params={"phone": phone},
    )
    if not res.result_set:
        return None
    row = res.result_set[0]
    return {"name": row[0], "budget": row[1], "segment": row[2], "phone": phone}


def customer_history(graph, phone, limit=5):
    """Past conversations + tickets for a customer (memory across sessions)."""
    if _is_memory(graph):
        return graph.customer_history(phone, limit)
    res = graph.query(
        """
        MATCH (c:Customer {phone: $phone})-[:HAD]->(conv:Conversation)
        RETURN conv.timestamp, conv.summary, conv.intent
        ORDER BY conv.timestamp DESC LIMIT $limit
        """,
        params={"phone": phone, "limit": limit},
    )
    convs = [
        {"timestamp": r[0], "summary": r[1], "intent": r[2]} for r in res.result_set
    ]
    res2 = graph.query(
        """
        MATCH (c:Customer {phone: $phone})-[:REPORTED]->(t:Ticket)
        RETURN t.id, t.issue, t.status, t.priority
        ORDER BY t.id DESC LIMIT $limit
        """,
        params={"phone": phone, "limit": limit},
    )
    tickets = [
        {"id": r[0], "issue": r[1], "status": r[2], "priority": r[3]}
        for r in res2.result_set
    ]
    return {"conversations": convs, "tickets": tickets}


def vehicles_in_budget(graph, budget, limit=5):
    if _is_memory(graph):
        return graph.vehicles_in_budget(budget, limit)
    res = graph.query(
        """
        MATCH (v:Vehicle)
        WHERE v.price <= $budget AND v.status = 'available'
        RETURN v.stock_no, v.make, v.model, v.year, v.price
        ORDER BY v.price DESC LIMIT $limit
        """,
        params={"budget": budget, "limit": limit},
    )
    return [
        {"stock_no": r[0], "make": r[1], "model": r[2], "year": r[3], "price": r[4]}
        for r in res.result_set
    ]


def find_policy(graph, topic_keyword):
    if _is_memory(graph):
        return graph.find_policy(topic_keyword)
    res = graph.query(
        """
        MATCH (p:Policy)
        WHERE toLower(p.topic) CONTAINS toLower($kw) OR toLower(p.content) CONTAINS toLower($kw)
        RETURN p.topic, p.content LIMIT 3
        """,
        params={"kw": topic_keyword},
    )
    return [{"topic": r[0], "content": r[1]} for r in res.result_set]


def churn_risk_customers(graph, limit=5):
    """High-value customers with unresolved high-priority tickets (agent ACTS on this)."""
    if _is_memory(graph):
        return graph.churn_risk_customers(limit)
    res = graph.query(
        """
        MATCH (c:Customer)-[:REPORTED]->(t:Ticket)
        WHERE t.status <> 'resolved' AND t.priority = 'high'
        WITH c, count(t) AS open_tickets
        RETURN c.name, c.phone, c.segment, open_tickets
        ORDER BY open_tickets DESC LIMIT $limit
        """,
        params={"limit": limit},
    )
    return [
        {"name": r[0], "phone": r[1], "segment": r[2], "open_tickets": r[3]}
        for r in res.result_set
    ]


def record_conversation(graph, phone, name, summary, intent):
    """Write memory back to the graph after each turn."""
    if _is_memory(graph):
        graph.record_conversation(phone, name, summary, intent)
        return
    graph.query(
        """
        MERGE (c:Customer {phone: $phone})
        ON CREATE SET c.name = $name, c.segment = 'new'
        CREATE (conv:Conversation {timestamp: timestamp(), summary: $summary, intent: $intent})
        CREATE (c)-[:HAD]->(conv)
        """,
        params={"phone": phone, "name": name, "summary": summary, "intent": intent},
    )


def create_ticket(graph, phone, issue, priority="normal"):
    if _is_memory(graph):
        return graph.create_ticket(phone, issue, priority)
    res = graph.query(
        """
        MATCH (c:Customer {phone: $phone})
        CREATE (t:Ticket {id: 'T' + toString(timestamp()),
                          issue: $issue, status: 'open',
                          priority: $priority, created: timestamp()})
        CREATE (c)-[:REPORTED]->(t)
        RETURN t.id
        """,
        params={"phone": phone, "issue": issue, "priority": priority},
    )
    return res.result_set[0][0] if res.result_set else None
