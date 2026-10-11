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


@app.on_event("startup")
def _startup():
    graph, _ = G.get_graph()
    G.ensure_schema(graph)


@app.get("/", response_class=HTMLResponse)
def index():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "static", "index.html")) as f:
        return f.read()


@app.get("/api/status")
def status():
    _, mode = G.get_graph()
    return {
        "falkordb_mode": mode,          # embedded | external — always a real engine
        "graph": G.GRAPH_NAME,
        "llm_provider": llm.provider_name(),
        "demo_data": True,
        "demo_data_note": "All customers, vehicles, tickets are FICTIONAL (app/seed.py).",
    }


@app.post("/api/chat")
def chat(body: ChatIn):
    graph, _ = G.get_graph()
    result = agents.handle_message(graph, body.phone.strip(),
                                   body.name.strip(), body.message.strip())
    return JSONResponse(result)


@app.get("/api/churn")
def churn():
    graph, _ = G.get_graph()
    return JSONResponse(agents.churn_report(graph))


@app.get("/api/graph/stats")
def graph_stats():
    graph, _ = G.get_graph()
    res = graph.query("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS c ORDER BY label")
    return {"counts": {r[0]: r[1] for r in res.result_set}}


@app.post("/api/seed")
def reseed():
    seedmod.seed()
    return {"ok": True, "note": "reseeded with FICTIONAL demo data"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8125")))
