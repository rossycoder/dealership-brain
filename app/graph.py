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
        # Just clear; seed() will re-seed fully via its own CREATE queries.
        # (Do NOT call graph._seed() here — that would duplicate everything.)
        graph._nodes.clear()
        graph._rels.clear()
        graph._next_id = 1
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


def vehicles_in_budget(graph, budget, limit=5, body_type=None, colour=None):
    if _is_memory(graph):
        return graph.vehicles_in_budget(budget, limit, body_type=body_type, colour=colour)
    clauses = ["v.price <= $budget", "v.status = 'available'"]
    params = {"budget": budget, "limit": limit}
    if body_type:
        clauses.append("toLower(v.body_type) = toLower($body_type)")
        params["body_type"] = body_type
    if colour:
        clauses.append("toLower(v.colour) = toLower($colour)")
        params["colour"] = colour
    res = graph.query(
        f"""
        MATCH (v:Vehicle)
        WHERE {" AND ".join(clauses)}
        RETURN v.stock_no, v.make, v.model, v.year, v.price, v.colour, v.body_type
        ORDER BY v.price DESC LIMIT $limit
        """,
        params=params,
    )
    return [
        {"stock_no": r[0], "make": r[1], "model": r[2], "year": r[3], "price": r[4],
         "colour": r[5], "body_type": r[6]}
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
                          issue: $issue, status: 'open', stage: 'received',
                          priority: $priority, created: timestamp()})
        CREATE (c)-[:REPORTED]->(t)
        RETURN t.id
        """,
        params={"phone": phone, "issue": issue, "priority": priority},
    )
    return res.result_set[0][0] if res.result_set else None


# ---------------------------------------------------------------------------
# Callbacks / Promises / Ticket stages (new features)
# ---------------------------------------------------------------------------

TICKET_STAGES = ["received", "diagnosing", "parts_ordered",
                 "in_progress", "quality_check", "ready"]

STAGE_MESSAGES = {
    "received": "Hi {name}, we've received your car for: {issue}. Ticket {id}. We'll keep you posted at every step. — Velocity Motors",
    "diagnosing": "Hi {name}, our technicians are now diagnosing your car (Ticket {id}). We'll update you once we know more. — Velocity Motors",
    "parts_ordered": "Hi {name}, we've ordered the parts needed for your car (Ticket {id}). We'll notify you when they arrive. — Velocity Motors",
    "in_progress": "Hi {name}, repair work on your car has started (Ticket {id}). Our team is on it. — Velocity Motors",
    "quality_check": "Hi {name}, your car (Ticket {id}) is in final quality check. Almost ready! — Velocity Motors",
    "ready": "Hi {name}, great news! Your car is ready for pickup (Ticket {id}). Please visit us at your convenience. — Velocity Motors",
}


def add_callback(graph, customer_phone, customer_name, staff, topic, promised_at):
    if _is_memory(graph):
        return graph.add_callback(customer_phone, customer_name, staff, topic, promised_at)
    import time as _t
    cb_id = f"CB-{int(_t.time() * 1000)}"
    graph.query(
        """CREATE (:Callback {id: $id, customer_phone: $phone, customer_name: $name,
                              staff: $staff, topic: $topic, promised_at: $promised_at,
                              created_at: timestamp(), status: 'pending'})""",
        params={"id": cb_id, "phone": customer_phone, "name": customer_name,
                "staff": staff, "topic": topic, "promised_at": promised_at},
    )
    return cb_id


def list_callbacks(graph):
    import time as _t
    now = int(_t.time() * 1000)
    if _is_memory(graph):
        return graph.list_callbacks()
    res = graph.query(
        """MATCH (c:Callback) RETURN c.id, c.customer_phone, c.customer_name,
           c.staff, c.topic, c.promised_at, c.created_at, c.status,
           c.done_at, c.done_on_time ORDER BY c.promised_at""")
    out = []
    for r in res.result_set:
        p = {"id": r[0], "customer_phone": r[1], "customer_name": r[2], "staff": r[3],
             "topic": r[4], "promised_at": r[5], "created_at": r[6], "status": r[7],
             "done_at": r[8], "done_on_time": r[9]}
        if p["status"] == "pending" and (p["promised_at"] or 0) < now:
            p["overdue"] = True
            p["overdue_ms"] = now - p["promised_at"]
        else:
            p["overdue"] = False
            p["due_in_ms"] = max(0, (p["promised_at"] or 0) - now)
        out.append(p)
    return out


def complete_callback(graph, cb_id):
    if _is_memory(graph):
        return graph.complete_callback(cb_id)
    res = graph.query(
        """MATCH (c:Callback {id: $id})
           SET c.status = 'done', c.done_at = timestamp(),
               c.done_on_time = (timestamp() <= c.promised_at)
           RETURN c.id, c.done_on_time""",
        params={"id": cb_id})
    if not res.result_set:
        return None
    return {"id": res.result_set[0][0], "done_on_time": res.result_set[0][1]}


def callback_stats(graph):
    import time as _t
    now = int(_t.time() * 1000)
    cbs = list_callbacks(graph)
    total = len(cbs)
    done = [c for c in cbs if c["status"] == "done"]
    on_time = [c for c in done if c.get("done_on_time")]
    overdue_now = [c for c in cbs if c.get("overdue")]
    return {"total": total, "done": len(done), "on_time": len(on_time),
            "compliance_pct": round(100 * len(on_time) / len(done), 1) if done else 100.0,
            "overdue_now": len(overdue_now)}


def add_promise(graph, customer_phone, customer_name, staff, promise, deadline_at):
    if _is_memory(graph):
        return graph.add_promise(customer_phone, customer_name, staff, promise, deadline_at)
    import time as _t
    pr_id = f"PR-{int(_t.time() * 1000)}"
    graph.query(
        """CREATE (:Promise {id: $id, customer_phone: $phone, customer_name: $name,
                             staff: $staff, promise: $promise, deadline_at: $deadline_at,
                             created_at: timestamp(), status: 'pending'})""",
        params={"id": pr_id, "phone": customer_phone, "name": customer_name,
                "staff": staff, "promise": promise, "deadline_at": deadline_at},
    )
    return pr_id


def list_promises(graph):
    import time as _t
    now = int(_t.time() * 1000)
    if _is_memory(graph):
        return graph.list_promises()
    res = graph.query(
        """MATCH (p:Promise) RETURN p.id, p.customer_phone, p.customer_name,
           p.staff, p.promise, p.deadline_at, p.created_at, p.status
           ORDER BY p.deadline_at""")
    out = []
    for r in res.result_set:
        p = {"id": r[0], "customer_phone": r[1], "customer_name": r[2], "staff": r[3],
             "promise": r[4], "deadline_at": r[5], "created_at": r[6], "status": r[7]}
        if p["status"] == "pending" and (p["deadline_at"] or 0) < now:
            p["overdue"] = True
            p["overdue_ms"] = now - p["deadline_at"]
        else:
            p["overdue"] = False
            p["due_in_ms"] = max(0, (p["deadline_at"] or 0) - now)
        out.append(p)
    return out


def complete_promise(graph, pr_id):
    if _is_memory(graph):
        return graph.complete_promise(pr_id)
    res = graph.query(
        """MATCH (p:Promise {id: $id})
           SET p.status = 'done', p.done_at = timestamp()
           RETURN p.id""",
        params={"id": pr_id})
    return {"id": res.result_set[0][0]} if res.result_set else None


def list_tickets(graph):
    if _is_memory(graph):
        return graph.list_tickets()
    res = graph.query(
        """MATCH (c:Customer)-[:REPORTED]->(t:Ticket)
           RETURN t.id, t.issue, t.status, t.priority, t.stage,
                  c.name, c.phone ORDER BY t.created DESC""")
    return [{"id": r[0], "issue": r[1], "status": r[2], "priority": r[3],
             "stage": r[4] or "received", "customer_name": r[5],
             "customer_phone": r[6]} for r in res.result_set]


def advance_ticket(graph, ticket_id):
    """Move a ticket to the next service stage; returns stage + customer message."""
    if _is_memory(graph):
        return graph.advance_ticket(ticket_id)
    # Cypher path: read current, compute next in Python, write back
    res = graph.query(
        """MATCH (c:Customer)-[:REPORTED]->(t:Ticket {id: $id})
           RETURN t.stage, t.issue, c.name""",
        params={"id": ticket_id})
    if not res.result_set:
        return None
    cur, issue, name = res.result_set[0]
    cur = cur or "received"
    try:
        idx = TICKET_STAGES.index(cur)
    except ValueError:
        idx = 0
    nxt = TICKET_STAGES[min(idx + 1, len(TICKET_STAGES) - 1)]
    new_status = "resolved" if nxt == "ready" else "open"
    graph.query(
        "MATCH (t:Ticket {id: $id}) SET t.stage = $stage, t.status = $status",
        params={"id": ticket_id, "stage": nxt, "status": new_status})
    msg = STAGE_MESSAGES[nxt].format(name=name or "there", issue=issue or "", id=ticket_id)
    return {"id": ticket_id, "stage": nxt, "stages": TICKET_STAGES,
            "customer_message": msg}
