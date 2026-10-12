"""Dealership Brain — FastAPI demo app.

Endpoints:
  GET  /                      -> chat UI
  POST /api/chat               {phone, name, message} -> agent reply + trace
  GET  /api/churn              -> churn-risk report (manager view)
  GET  /api/graph/stats        -> node counts per label (proves real graph)
  POST /api/seed                -> reseed fictional demo data
  GET  /api/status             -> db mode, llm provider (honest capabilities)

All data is FICTIONAL demo data (see app/seed.py). UI says so.
"""
import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from app import graph as G
from app import agents, seed as seedmod
from app import llm

app = FastAPI(title="Dealership Brain — Graph Hacks prep")


class ChatIn(BaseModel):
    phone: str
    name: str = ""
    message: str


class CallbackIn(BaseModel):
    customer_phone: str
    customer_name: str = ""
    staff: str = "Usman Tariq"
    topic: str = ""
    promised_in_hours: float = 2.0  # demo-friendly: hours from now (can be negative for overdue demo)


class PromiseIn(BaseModel):
    customer_phone: str
    customer_name: str = ""
    staff: str = "Bilal Ahmed"
    promise: str = ""
    deadline_in_days: float = 3.0


@app.on_event("startup")
def _startup():
    # Defensive: never crash the function at startup (Vercel runs this even
    # with Mangum lifespan="off"). Graph errors are reported via /api/status.
    try:
        graph, _ = G.get_graph()
        G.ensure_schema(graph)
    except Exception:
        pass  # /api/status will show the diagnostic


@app.get("/", response_class=HTMLResponse)
def index():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "static", "index.html")) as f:
        return f.read()


# Vercel rewrite target: when "/" is rewritten to "/api/index", serve the UI.
# (Some Vercel Python runtimes don't normalize the path before Mangum sees it.)
@app.get("/api/index", response_class=HTMLResponse)
def index_via_rewrite():
    return index()


@app.get("/api/status")
def status():
    _, mode = G.get_graph()
    out = {
        "falkordb_mode": mode,  # embedded | external | memory (fallback)
        "graph": G.GRAPH_NAME,
        "llm_provider": llm.provider_name(),
        "demo_data": True,
        "demo_data_note": "All customers, vehicles, tickets are FICTIONAL (app/seed.py).",
    }
    if mode == "memory":
        out["note"] = "In-memory graph fallback (redislite unavailable on this host). Same data model, demo fully functional."
    err = G.get_last_error()
    if err:
        out["diagnostic"] = err[:500]
    return out


def _need_graph():
    """Return graph (never crashes — get_graph falls back to memory mode)."""
    graph, _ = G.get_graph()
    return graph


@app.post("/api/chat")
def chat(body: ChatIn):
    graph = _need_graph()
    result = agents.handle_message(graph, body.phone.strip(),
                                   body.name.strip(), body.message.strip())
    return JSONResponse(result)


@app.get("/api/churn")
def churn():
    graph = _need_graph()
    return JSONResponse(agents.churn_report(graph))


@app.get("/api/graph/stats")
def graph_stats():
    graph = _need_graph()
    if G._is_memory(graph):
        return {"counts": graph.stats(), "mode": "memory"}
    res = graph.query("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS c ORDER BY label")
    return {"counts": {r[0]: r[1] for r in res.result_set}}


@app.post("/api/seed")
def reseed():
    seedmod.seed()
    return {"ok": True, "note": "reseeded with FICTIONAL demo data"}


# ---------------------------------------------------------------------------
# Callback SLA Tracker — "nobody returns my calls" (real complaint #1)
# ---------------------------------------------------------------------------

@app.post("/api/callbacks")
def create_callback(body: CallbackIn):
    import time
    graph = _need_graph()
    promised_at = int(time.time() * 1000 + body.promised_in_hours * 3600 * 1000)
    cb_id = G.add_callback(graph, body.customer_phone.strip(),
                           body.customer_name.strip() or body.customer_phone.strip(),
                           body.staff.strip(), body.topic.strip(), promised_at)
    return {"ok": True, "id": cb_id}


@app.get("/api/callbacks")
def get_callbacks():
    graph = _need_graph()
    return {"callbacks": G.list_callbacks(graph)}


@app.post("/api/callbacks/{cb_id}/done")
def done_callback(cb_id: str):
    graph = _need_graph()
    res = G.complete_callback(graph, cb_id)
    if res is None:
        return JSONResponse({"ok": False, "error": "callback not found"}, status_code=404)
    return {"ok": True, **res}


@app.get("/api/callbacks/stats")
def get_callback_stats():
    graph = _need_graph()
    return G.callback_stats(graph)


# ---------------------------------------------------------------------------
# Promise Tracker — "promised a spare key, 24 days, nothing"
# ---------------------------------------------------------------------------

@app.post("/api/promises")
def create_promise(body: PromiseIn):
    import time
    graph = _need_graph()
    deadline_at = int(time.time() * 1000 + body.deadline_in_days * 24 * 3600 * 1000)
    pr_id = G.add_promise(graph, body.customer_phone.strip(),
                          body.customer_name.strip() or body.customer_phone.strip(),
                          body.staff.strip(), body.promise.strip(), deadline_at)
    return {"ok": True, "id": pr_id}


@app.get("/api/promises")
def get_promises():
    graph = _need_graph()
    return {"promises": G.list_promises(graph)}


@app.post("/api/promises/{pr_id}/done")
def done_promise(pr_id: str):
    graph = _need_graph()
    res = G.complete_promise(graph, pr_id)
    if res is None:
        return JSONResponse({"ok": False, "error": "promise not found"}, status_code=404)
    return {"ok": True, **res}


# ---------------------------------------------------------------------------
# Service Progress Updates — "car 4 days in workshop, zero updates"
# ---------------------------------------------------------------------------

@app.get("/api/tickets")
def get_tickets():
    graph = _need_graph()
    return {"tickets": G.list_tickets(graph), "stages": G.TICKET_STAGES}


@app.post("/api/tickets/{ticket_id}/advance")
def advance_ticket(ticket_id: str):
    graph = _need_graph()
    res = G.advance_ticket(graph, ticket_id)
    if res is None:
        return JSONResponse({"ok": False, "error": "ticket not found"}, status_code=404)
    return {"ok": True, **res}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8125")))
