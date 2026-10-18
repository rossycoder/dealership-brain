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


# Per-tenant memory graphs (SaaS workspaces) — slug -> MemoryGraph
_tenant_memory_graphs = {}


def _get_tenant_memory_graph(slug):
    g = _tenant_memory_graphs.get(slug)
    if g is None:
        from app.memory_graph import MemoryGraph
        g = MemoryGraph(seed_demo=False)
        _tenant_memory_graphs[slug] = g
    return g


def get_tenant_graph(slug):
    """Return (graph, mode) for a SaaS workspace (tenant).

    'demo' (or empty) = the original single demo graph (get_graph()).
    Any other slug gets its OWN graph — FalkorDB: a separate named graph
    'dealership_<slug>'; memory fallback: a separate EMPTY MemoryGraph.
    Tenant graphs are NEVER seeded with the fictional demo dataset;
    app.seed.seed_workspace() gives a new workspace its starter kit.
    """
    if not slug or slug == "demo":
        return get_graph()
    try:
        db, mode = get_db()
    except Exception:
        return _get_tenant_memory_graph(slug), "memory"
    if mode == "memory":
        return _get_tenant_memory_graph(slug), "memory"
    graph = db.select_graph("dealership_" + slug)
    try:
        ensure_schema(graph)
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


# ---------------------------------------------------------------------------
# Neighborhood graph / Smart Match / Staff leaderboard (advisor features)
# ---------------------------------------------------------------------------

def neighborhood(graph, phone):
    """Customer-centered subgraph -> {nodes, edges} for vis.js."""
    if _is_memory(graph):
        return graph.neighborhood(phone)
    cust = find_customer_by_phone(graph, phone)
    if not cust:
        return {"nodes": [], "edges": [], "error": "customer not found"}
    nodes, edges, seen = [], [], set()

    def add_node(key, group, label, title):
        if key not in seen:
            seen.add(key)
            nodes.append({"id": key, "group": group, "label": label, "title": title})

    def add_edge(frm, to, label):
        edges.append({"from": frm, "to": to, "label": label})

    c_key = f"c:{phone}"
    add_node(c_key, "customer", cust["name"] or phone,
             f"Customer: {cust['name']}<br>Phone: {phone}<br>"
             f"Budget: PKR {(cust.get('budget') or 0):,}<br>Segment: {cust.get('segment')}")
    res = graph.query(
        """MATCH (c:Customer {phone: $phone})-[r]->(n)
           RETURN labels(n)[0], n, type(r)""",
        params={"phone": phone})

    def _props(node):
        # falkordb-py returns Node objects; be liberal in what we accept
        if hasattr(node, "properties"):
            return dict(node.properties)
        if isinstance(node, dict):
            return dict(node)
        try:
            return dict(node)
        except Exception:
            return {}

    for r in res.result_set:
        label, props, rtype = r[0], _props(r[1]), r[2]
        key = None
        if label == "Vehicle":
            key = f"v:{props.get('stock_no')}"
            add_node(key, "vehicle", f"{props.get('make')} {props.get('model')}",
                     f"{props.get('year')} {props.get('make')} {props.get('model')}<br>"
                     f"PKR {(props.get('price') or 0):,} · {props.get('colour')} · {props.get('body_type')}")
        elif label == "Ticket":
            key = f"t:{props.get('id')}"
            add_node(key, "ticket", f"🎫 {props.get('id')}",
                     f"Ticket {props.get('id')}: {props.get('issue')}<br>Status: {props.get('status')}")
        elif label == "Conversation":
            key = f"cv:{props.get('timestamp')}"
            add_node(key, "conversation", f"💬 {str(props.get('summary') or '')[:28]}",
                     f"Conversation: {props.get('summary')}<br>Intent: {props.get('intent')}")
        if key:
            add_edge(c_key, key, rtype)
    # callbacks + promises linked by phone
    for lbl, keypre, group, icon in (("Callback", "cb", "callback", "📞"),
                                     ("Promise", "pr", "promise", "🤝")):
        res2 = graph.query(
            f"""MATCH (x:{lbl}) WHERE x.customer_phone = $phone
                RETURN x.id, x.topic, x.promise, x.staff, x.status""",
            params={"phone": phone})
        for r in res2.result_set:
            key = f"{keypre}:{r[0]}"
            txt = r[1] or r[2] or ""
            add_node(key, group, f"{icon} {str(txt)[:24]}",
                     f"{lbl}: {txt}<br>Staff: {r[3]} · Status: {r[4]}")
            add_edge(c_key, key, "OWES" if lbl == "Callback" else "PROMISED")
    return {"nodes": nodes, "edges": edges,
            "customer": {"name": cust["name"], "phone": phone}}


def list_vehicles(graph):
    if _is_memory(graph):
        return graph.list_vehicles()
    res = graph.query(
        """MATCH (v:Vehicle) RETURN v.stock_no, v.make, v.model, v.year,
           v.price, v.colour, v.body_type, v.status ORDER BY v.stock_no""")
    return [{"stock_no": r[0], "make": r[1], "model": r[2], "year": r[3],
             "price": r[4], "colour": r[5], "body_type": r[6], "status": r[7]}
            for r in res.result_set]


def add_vehicle(graph, stock_no, make, model, year, price, colour, body_type,
                status="available"):
    if _is_memory(graph):
        return graph.add_vehicle(stock_no, make, model, year, price,
                                 colour, body_type, status)
    graph.query(
        """CREATE (:Vehicle {stock_no: $stock_no, make: $make, model: $model,
                             year: $year, price: $price, colour: $colour,
                             body_type: $body_type, status: $status})""",
        params={"stock_no": stock_no, "make": make, "model": model, "year": year,
                "price": price, "colour": colour, "body_type": body_type,
                "status": status})
    return stock_no


def smart_match(graph, stock_no):
    """Rank customers for a vehicle. Cypher path mirrors memory logic."""
    if _is_memory(graph):
        return graph.smart_match(stock_no)
    vehs = [v for v in list_vehicles(graph) if v["stock_no"] == stock_no]
    if not vehs:
        return {"vehicle": None, "matches": [], "error": "vehicle not found"}
    veh = vehs[0]
    price, v_body = veh.get("price") or 0, (veh.get("body_type") or "").lower()
    v_make = (veh.get("make") or "").lower()
    res = graph.query(
        """MATCH (c:Customer)
           OPTIONAL MATCH (c)-[r:INTERESTED_IN|VIEWED]->(v:Vehicle)
           OPTIONAL MATCH (c)-[:OWNS]->(owned:Vehicle {stock_no: $stock_no})
           OPTIONAL MATCH (c)-[:HAD]->(conv:Conversation)
           RETURN c.name, c.phone, c.budget, c.segment,
                  collect(DISTINCT {rel: type(r), body: v.body_type, make: v.make}) AS iv,
                  count(DISTINCT owned) AS owns,
                  count(DISTINCT conv) AS convs""",
        params={"stock_no": stock_no})
    matches = []
    for r in res.result_set:
        name, phone, budget, segment, iv, owns, convs = r[0], r[1], r[2] or 0, r[3], r[4] or [], r[5] or 0, r[6] or 0
        if owns:
            continue
        if price <= budget:
            b_score, b_reason = 40, f"within budget (PKR {budget:,})"
        elif price <= budget * 1.15:
            b_score, b_reason = 20, f"slightly over budget (PKR {budget:,})"
        else:
            b_score, b_reason = 0, f"over budget (PKR {budget:,})"
        i_score, i_reasons = 0, []
        for item in iv:
            if not item.get("rel"):
                continue
            if (item.get("body") or "").lower() == v_body and v_body:
                i_score = max(i_score, 25 if item["rel"] == "INTERESTED_IN" else 20)
                i_reasons.append(f"{item['rel'].lower()} a {v_body} before")
            if (item.get("make") or "").lower() == v_make and v_make:
                i_score = max(i_score, 15 if item["rel"] == "INTERESTED_IN" else 12)
                i_reasons.append(f"{item['rel'].lower()} {veh.get('make')} before")
        e_score = 20 if convs else 0
        total = b_score + i_score + e_score
        if total <= 0:
            continue
        matches.append({"name": name, "phone": phone, "budget": budget,
                        "segment": segment, "score": total,
                        "reasons": [b_reason] + i_reasons +
                                   [f"{convs} past conversation(s)" if convs else "no prior conversations"]})
    matches.sort(key=lambda m: m["score"], reverse=True)
    return {"vehicle": veh, "matches": matches[:8]}


def staff_leaderboard(graph):
    if _is_memory(graph):
        return graph.staff_leaderboard()
    import time as _t
    now = int(_t.time() * 1000)
    res = graph.query("MATCH (s:Staff) RETURN s.name, s.role")
    staff = {r[0]: {"name": r[0], "role": r[1], "cb_total": 0, "cb_done": 0,
                    "cb_on_time": 0, "pr_total": 0, "pr_kept": 0, "pr_overdue": 0}
             for r in res.result_set}
    res = graph.query("MATCH (c:Callback) RETURN c.staff, c.status, c.done_on_time, c.promised_at")
    for r in res.result_set:
        s = staff.setdefault(r[0], {"name": r[0], "role": "", "cb_total": 0, "cb_done": 0,
                                    "cb_on_time": 0, "pr_total": 0, "pr_kept": 0, "pr_overdue": 0})
        s["cb_total"] += 1
        if r[1] == "done":
            s["cb_done"] += 1
            if r[2]:
                s["cb_on_time"] += 1
    res = graph.query("MATCH (p:Promise) RETURN p.staff, p.status, p.deadline_at")
    for r in res.result_set:
        s = staff.setdefault(r[0], {"name": r[0], "role": "", "cb_total": 0, "cb_done": 0,
                                    "cb_on_time": 0, "pr_total": 0, "pr_kept": 0, "pr_overdue": 0})
        s["pr_total"] += 1
        if r[1] == "done":
            s["pr_kept"] += 1
        elif (r[2] or 0) < now:
            s["pr_overdue"] += 1
    board = list(staff.values())
    for s in board:
        s["score"] = s["cb_on_time"] * 10 + s["pr_kept"] * 10 + s["cb_done"] * 2
        s["cb_compliance"] = round(100 * s["cb_on_time"] / s["cb_done"], 1) if s["cb_done"] else 100.0
    board.sort(key=lambda s: s["score"], reverse=True)
    return {"leaderboard": board}


# ---------------------------------------------------------------------------
# Trade-in valuation / Finance pre-check (pure calculation, no graph needed)
# ---------------------------------------------------------------------------

TRADEIN_BASE_PRICES = {
    # (make, model) -> approx new price PKR (fictional demo reference values)
    ("toyota", "corolla"): 2850000,
    ("honda", "civic"): 4200000,
    ("kia", "sportage"): 4950000,
    ("suzuki", "swift"): 1950000,
    ("toyota", "fortuner"): 8900000,
    ("honda", "city"): 2400000,
    ("hyundai", "tucson"): 4800000,
    ("toyota", "yaris"): 2300000,
}
MAKE_MULTIPLIER = {"toyota": 1.10, "honda": 1.05, "kia": 0.95,
                   "hyundai": 0.95, "suzuki": 0.80}
CONDITION_MULT = {"excellent": 1.00, "good": 0.92, "fair": 0.80, "poor": 0.62}


def valuate_tradein(make, model, year, mileage_km, condition="good"):
    """Transparent trade-in estimate. Returns low/mid/high + calculation breakdown."""
    import time as _t
    mk, md = (make or "").strip().lower(), (model or "").strip().lower()
    cond = (condition or "good").strip().lower()
    if cond not in CONDITION_MULT:
        cond = "good"
    base = TRADEIN_BASE_PRICES.get((mk, md))
    if base is None:
        base = 2500000 * MAKE_MULTIPLIER.get(mk, 0.90)
        base_note = f"estimated base for {make} {model} (not in reference table)"
    else:
        base_note = f"reference new price for {make} {model}"
    cur_year = _t.gmtime().tm_year
    age = max(0, cur_year - int(year or cur_year))
    dep_factor = 0.88 ** age
    expected_km = age * 15000
    mileage_km = max(0, int(mileage_km or 0))
    # Low mileage bonus / high mileage penalty, clamped
    mileage_factor = max(0.70, min(1.10, 1.0 - (mileage_km - expected_km) / 400000))
    cond_factor = CONDITION_MULT[cond]
    mid = base * dep_factor * mileage_factor * cond_factor
    steps = [
        f"Base ({base_note}): PKR {base:,.0f}",
        f"Age depreciation: {age} yr × 12%/yr → factor {dep_factor:.3f}",
        f"Mileage adjustment: {mileage_km:,} km vs expected {expected_km:,} km → factor {mileage_factor:.3f}",
        f"Condition '{cond}' → factor {cond_factor:.2f}",
        f"Estimate = {base:,.0f} × {dep_factor:.3f} × {mileage_factor:.3f} × {cond_factor:.2f}",
    ]
    return {
        "make": make, "model": model, "year": year,
        "mileage_km": mileage_km, "condition": cond,
        "low": round(mid * 0.92), "mid": round(mid), "high": round(mid * 1.08),
        "breakdown": steps,
        "note": "Fictional demo estimate — final offer needs physical inspection.",
    }


def finance_quote(price, down_payment, months, annual_rate_pct=22.0):
    """EMI quote via standard reducing-balance formula."""
    price = float(price or 0)
    down = min(float(down_payment or 0), price)
    n = max(1, int(months or 12))
    principal = price - down
    r = (float(annual_rate_pct) or 0) / 100 / 12
    if r <= 0:
        emi = principal / n
    else:
        emi = principal * r * (1 + r) ** n / ((1 + r) ** n - 1)
    total = emi * n
    interest = total - principal
    first_interest = principal * r
    return {
        "price": round(price), "down_payment": round(down),
        "financed": round(principal), "months": n,
        "annual_rate_pct": annual_rate_pct,
        "emi": round(emi), "total_payable": round(total + down),
        "total_interest": round(interest),
        "first_month": {"interest": round(first_interest),
                        "principal": round(emi - first_interest)},
        "last_month_approx": {"emi": round(emi)},
        "note": "Demo quote at 22% p.a. reducing balance — bank terms may differ.",
    }


# ---------------------------------------------------------------------------
# Test-drive bookings
# ---------------------------------------------------------------------------

TESTDRIVE_SLOTS = ["10:00", "11:30", "13:00", "14:30", "16:00", "17:30"]


def book_testdrive(graph, customer_phone, customer_name, stock_no, date, time_slot):
    if _is_memory(graph):
        return graph.book_testdrive(customer_phone, customer_name, stock_no,
                                    date, time_slot)
    import time as _t
    if time_slot not in TESTDRIVE_SLOTS:
        return {"ok": False, "error": f"invalid slot (choose from {TESTDRIVE_SLOTS})"}
    veh = graph.query("MATCH (v:Vehicle {stock_no: $s}) RETURN v.stock_no",
                      params={"s": stock_no})
    if not veh.result_set:
        return {"ok": False, "error": "vehicle not found"}
    clash = graph.query(
        """MATCH (t:TestDrive {stock_no: $s, date: $d, time_slot: $slot})
           WHERE t.status = 'booked' RETURN t.id LIMIT 1""",
        params={"s": stock_no, "d": date, "slot": time_slot})
    if clash.result_set:
        return {"ok": False, "error": "slot already booked for this vehicle"}
    td_id = f"TD-{int(_t.time() * 1000)}"
    graph.query(
        """MERGE (c:Customer {phone: $phone})
           ON CREATE SET c.name = $name, c.segment = 'new'
           CREATE (t:TestDrive {id: $id, customer_phone: $phone, customer_name: $name,
                                stock_no: $s, date: $d, time_slot: $slot,
                                status: 'booked', created_at: timestamp()})
           CREATE (c)-[:BOOKED]->(t)""",
        params={"id": td_id, "phone": customer_phone, "name": customer_name,
                "s": stock_no, "d": date, "slot": time_slot})
    return {"ok": True, "id": td_id}


def list_testdrives(graph):
    if _is_memory(graph):
        return graph.list_testdrives()
    res = graph.query(
        """MATCH (t:TestDrive)
           OPTIONAL MATCH (v:Vehicle {stock_no: t.stock_no})
           RETURN t.id, t.customer_phone, t.customer_name, t.stock_no,
                  v.make, v.model, v.year, t.date, t.time_slot, t.status
           ORDER BY t.date, t.time_slot""")
    return [{"id": r[0], "customer_phone": r[1], "customer_name": r[2],
             "stock_no": r[3], "make": r[4], "model": r[5], "year": r[6],
             "date": r[7], "time_slot": r[8], "status": r[9]}
            for r in res.result_set]


# ---------------------------------------------------------------------------
# Service reminders
# ---------------------------------------------------------------------------

SERVICE_INTERVAL_DAYS = 180


def service_due(graph):
    """Vehicles due/overdue for service, most urgent first."""
    import time as _t
    now_ms = int(_t.time() * 1000)
    day = 24 * 3600 * 1000
    if _is_memory(graph):
        return graph.service_due()
    res = graph.query(
        """MATCH (v:Vehicle)
           OPTIONAL MATCH (c:Customer)-[:OWNS]->(v)
           RETURN v.stock_no, v.make, v.model, v.year, v.status,
                  v.last_service_date, v.service_interval_km,
                  c.name, c.phone""")
    out = []
    for r in res.result_set:
        last = r[5] or 0
        due_at = last + SERVICE_INTERVAL_DAYS * day if last else 0
        days_left = (due_at - now_ms) / day if due_at else 0
        out.append({
            "stock_no": r[0], "make": r[1], "model": r[2], "year": r[3],
            "status": r[4], "last_service_date": last,
            "service_interval_km": r[6] or 5000,
            "owner_name": r[7], "owner_phone": r[8],
            "days_overdue": round(-days_left) if days_left < 0 else 0,
            "due_in_days": round(days_left) if days_left >= 0 else 0,
            "urgency": "overdue" if days_left < 0 else
                       "due_soon" if days_left <= 30 else "ok",
        })
    out.sort(key=lambda v: (0 if v["urgency"] == "overdue" else
                            1 if v["urgency"] == "due_soon" else 2,
                            -v["days_overdue"]))
    return {"reminders": out, "interval_days": SERVICE_INTERVAL_DAYS}


# ---------------------------------------------------------------------------
# "Brain remembers" — full customer memory card
# ---------------------------------------------------------------------------

def customer_memory(graph, phone):
    """Everything the brain knows about a customer, in one card."""
    cust = find_customer_by_phone(graph, phone)
    if not cust:
        return {"error": "customer not found", "phone": phone}
    hist = customer_history(graph, phone)
    promises = [p for p in list_promises(graph)
                if p.get("customer_phone") == phone and p.get("status") == "pending"]
    callbacks = [c for c in list_callbacks(graph)
                 if c.get("customer_phone") == phone and c.get("status") == "pending"]
    if _is_memory(graph):
        veh = graph.customer_vehicles(phone)
    else:
        res = graph.query(
            """MATCH (c:Customer {phone: $phone})-[r:INTERESTED_IN|VIEWED|OWNS]->(v:Vehicle)
               RETURN type(r), v.stock_no, v.make, v.model, v.year, v.price,
                      v.colour, v.body_type""",
            params={"phone": phone})
        veh = {"interested_in": [], "viewed": [], "owns": []}
        for r in res.result_set:
            entry = {"stock_no": r[1], "make": r[2], "model": r[3], "year": r[4],
                     "price": r[5], "colour": r[6], "body_type": r[7]}
            key = {"INTERESTED_IN": "interested_in", "VIEWED": "viewed",
                   "OWNS": "owns"}.get(r[0])
            if key:
                veh[key].append(entry)
    # inferred preferences
    bodies, makes = {}, {}
    for bucket in ("interested_in", "viewed"):
        for v in veh.get(bucket, []):
            b, m = (v.get("body_type") or "").lower(), (v.get("make") or "").lower()
            if b:
                bodies[b] = bodies.get(b, 0) + 1
            if m:
                makes[m] = makes.get(m, 0) + 1
    inferred = {
        "preferred_body_types": sorted(bodies, key=bodies.get, reverse=True)[:3],
        "preferred_makes": sorted(makes, key=makes.get, reverse=True)[:3],
    }
    return {
        "customer": cust,
        "vehicles": veh,
        "tickets": hist["tickets"],
        "conversations": hist["conversations"],
        "pending_promises": promises,
        "pending_callbacks": callbacks,
        "inferred_preferences": inferred,
        "memory_note": "Every chat turn is written back to the graph as a Conversation node.",
    }


# ---------------------------------------------------------------------------
# Alert log — audit trail for SLA escalations (Alert nodes, both modes)
# ---------------------------------------------------------------------------

def record_alert(graph, event, ref_id, customer_name, customer_phone, staff,
                 reason, outcome, http_status=0, message=""):
    """Persist one escalation attempt so the manager can see who was alerted,
    when, and what happened. outcome: webhook_sent | webhook_preview | webhook_failed."""
    import time as _t
    if _is_memory(graph):
        return graph.record_alert(event, ref_id, customer_name, customer_phone,
                                  staff, reason, outcome, http_status, message)
    al_id = f"AL-{int(_t.time() * 1000)}"
    graph.query(
        """CREATE (:Alert {id: $id, event: $event, ref_id: $ref_id,
                           customer_name: $customer_name, customer_phone: $customer_phone,
                           staff: $staff, reason: $reason, outcome: $outcome,
                           http_status: $http_status, message: $message,
                           created_at: timestamp()})""",
        params={"id": al_id, "event": event, "ref_id": ref_id,
                "customer_name": customer_name, "customer_phone": customer_phone,
                "staff": staff, "reason": reason, "outcome": outcome,
                "http_status": http_status, "message": message},
    )
    return al_id


def list_alerts(graph, limit=50):
    """Newest-first escalation history for the manager's Alert Log."""
    if _is_memory(graph):
        return graph.list_alerts(limit)
    res = graph.query(
        """MATCH (a:Alert) RETURN a.id, a.event, a.ref_id, a.customer_name,
           a.customer_phone, a.staff, a.reason, a.outcome, a.http_status,
           a.message, a.created_at ORDER BY a.created_at DESC LIMIT $limit""",
        params={"limit": int(limit)})
    return [{"id": r[0], "event": r[1], "ref_id": r[2], "customer_name": r[3],
             "customer_phone": r[4], "staff": r[5], "reason": r[6],
             "outcome": r[7], "http_status": r[8], "message": r[9],
             "created_at": r[10]} for r in res.result_set]


def _fmt_overdue(overdue_ms):
    d, h = divmod(overdue_ms // 60000, 24 * 60)
    return f"{d}d {h // 60}h" if d else f"{h // 60}h {(h % 60)}m"


def _post_webhook(payload):
    """POST to N8N_WEBHOOK_URL when configured. Returns
    (outcome, http_status, note): note only set for preview mode."""
    import json as _json
    import urllib.request as _urlreq
    url = os.environ.get("N8N_WEBHOOK_URL", "").strip()
    if not url:
        return ("webhook_preview", 0,
                "N8N_WEBHOOK_URL not configured — showing payload preview")
    try:
        req = _urlreq.Request(url, data=_json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json"})
        with _urlreq.urlopen(req, timeout=8) as resp:
            return ("webhook_sent", resp.status, "")
    except Exception as e:
        return ("webhook_failed", 0,
                f"webhook POST failed: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# n8n webhook alerts for broken promises / missed callbacks
# ---------------------------------------------------------------------------

def send_promise_alert(graph, pr_id):
    """Build the n8n payload for an overdue promise; POST it if a webhook is
    configured; ALWAYS record the attempt in the Alert Log."""
    import time as _t
    target = next((p for p in list_promises(graph) if p.get("id") == pr_id), None)
    if not target:
        return {"ok": False, "error": "promise not found"}
    now = int(_t.time() * 1000)
    overdue_ms = max(0, now - (target.get("deadline_at") or now))
    wa_msg = (
        f"Hi {target.get('staff')}, this is Dealership Brain. Your promise to "
        f"{target.get('customer_name')} — “{target.get('promise')}” — is overdue by "
        f"{_fmt_overdue(overdue_ms)}. Please call the customer ({target.get('customer_phone')}) "
        f"today with an update. — Velocity Motors"
    )
    payload = {
        "event": "promise_overdue",
        "promise_id": pr_id,
        "customer": {"name": target.get("customer_name"),
                     "phone": target.get("customer_phone")},
        "staff": target.get("staff"),
        "promise": target.get("promise"),
        "deadline_at": target.get("deadline_at"),
        "overdue_ms": overdue_ms,
        "suggested_whatsapp_message": wa_msg,
        "source": "dealership-brain",
    }
    outcome, http_status, note = _post_webhook(payload)
    record_alert(graph, "promise_overdue", pr_id,
                 target.get("customer_name"), target.get("customer_phone"),
                 target.get("staff"),
                 f"Promise overdue by {_fmt_overdue(overdue_ms)}: “{target.get('promise')}”",
                 outcome, http_status, wa_msg)
    if outcome == "webhook_failed":
        return {"ok": False, "sent": False, "error": note, "payload": payload}
    return {"ok": True, "sent": outcome == "webhook_sent",
            "http_status": http_status,
            "reason": note or None, "payload": payload}


def send_callback_alert(graph, cb_id):
    """Build the n8n payload for a missed callback SLA; POST if configured;
    ALWAYS record the attempt in the Alert Log."""
    import time as _t
    target = next((c for c in list_callbacks(graph) if c.get("id") == cb_id), None)
    if not target:
        return {"ok": False, "error": "callback not found"}
    now = int(_t.time() * 1000)
    overdue_ms = max(0, now - (target.get("promised_at") or now))
    wa_msg = (
        f"Hi {target.get('staff')}, this is Dealership Brain. You promised to call "
        f"{target.get('customer_name')} about “{target.get('topic')}” — it is "
        f"overdue by {_fmt_overdue(overdue_ms)}. Please call {target.get('customer_phone')} "
        f"now. — Velocity Motors"
    )
    payload = {
        "event": "callback_overdue",
        "callback_id": cb_id,
        "customer": {"name": target.get("customer_name"),
                     "phone": target.get("customer_phone")},
        "staff": target.get("staff"),
        "topic": target.get("topic"),
        "promised_at": target.get("promised_at"),
        "overdue_ms": overdue_ms,
        "suggested_whatsapp_message": wa_msg,
        "source": "dealership-brain",
    }
    outcome, http_status, note = _post_webhook(payload)
    record_alert(graph, "callback_overdue", cb_id,
                 target.get("customer_name"), target.get("customer_phone"),
                 target.get("staff"),
                 f"Callback SLA missed by {_fmt_overdue(overdue_ms)}: “{target.get('topic')}”",
                 outcome, http_status, wa_msg)
    if outcome == "webhook_failed":
        return {"ok": False, "sent": False, "error": note, "payload": payload}
    return {"ok": True, "sent": outcome == "webhook_sent",
            "http_status": http_status,
            "reason": note or None, "payload": payload}
