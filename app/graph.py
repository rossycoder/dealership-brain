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
