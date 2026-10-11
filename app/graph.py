"""FalkorDB connection layer.

Two modes:
- EMBEDDED (default): real FalkorDB engine via falkordblite (redislite).
  No Docker, no server needed. Used for local dev and demo.
- EXTERNAL: connect to a real FalkorDB server (FalkorDB Cloud / Docker)
  when FALKOR_HOST is set. Used at hackathon time.

Both modes run REAL Cypher against a REAL FalkorDB engine. Nothing is faked.
"""
import os

GRAPH_NAME = "dealership_brain"


def get_db():
    host = os.environ.get("FALKOR_HOST")
    if host:
        from falkordb import FalkorDB
        port = int(os.environ.get("FALKOR_PORT", "6379"))
        password = os.environ.get("FALKOR_PASSWORD") or None
        db = FalkorDB(host=host, port=port, password=password)
        return db, "external"
    from redislite import FalkorDB
    # Persistent file so seed data survives across processes/restarts.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.environ.get("FALKOR_DATA_DIR", os.path.join(here, "falkordb-data"))
    os.makedirs(data_dir, exist_ok=True)
    db = FalkorDB(os.path.join(data_dir, "brain.db"))
    return db, "embedded"


_seeded_this_process = False


def get_graph(auto_seed=True):
    """Return (graph, mode). ensure_schema runs lazily (idempotent) so the app
    works even when the FastAPI startup event never fires (e.g. Mangum on Vercel).

    When auto_seed is True (default), an empty graph is seeded once per process
    with the fictional demo dataset — this makes cold starts on serverless
    (Vercel) immediately demoable. Pass auto_seed=False from seed() itself to
    avoid re-entry.
    """
    global _seeded_this_process
    db, mode = get_db()
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
    graph.query("MATCH (n) DETACH DELETE n")


SCHEMA_CYPHER = """
// Indexes for fast lookup (idempotent-ish; FalkorDB ignores duplicates gracefully)
CREATE INDEX FOR (c:Customer) ON (c.phone);
CREATE INDEX FOR (v:Vehicle) ON (v.stock_no);
"""


def ensure_schema(graph):
    for stmt in [s.strip() for s in SCHEMA_CYPHER.strip().split(";") if s.strip()]:
        try:
            graph.query(stmt)
        except Exception:
            pass  # index may already exist


# ---------------------------------------------------------------------------
# Read helpers used by agents (all real Cypher)
# ---------------------------------------------------------------------------

def find_customer_by_phone(graph, phone):
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
