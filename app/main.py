"""Dealership Brain — FastAPI demo app.

Endpoints:
  GET  /                      -> chat UI
  POST /api/chat               {phone, name, message} -> agent reply + trace
  GET  /api/churn              -> churn-risk report (manager view)
  GET  /api/promise/tracking   -> promise-completion status (read-only)
  POST /api/promise/watch      -> run Promise Watch agent (reminders + escalations)
  GET  /api/graph/stats        -> node counts per label (proves real graph)
  POST /api/seed                -> reseed fictional demo data
  GET  /api/status             -> db mode, llm provider (honest capabilities)

All data is FICTIONAL demo data (see app/seed.py). UI says so.
"""
import os
import time
from contextvars import ContextVar

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from app import graph as G
from app import accounts
from app import agents
from app import llm

app = FastAPI(title="Dealership Brain — Graph Hacks prep")

# --- SaaS tenancy -----------------------------------------------------------
# Every request belongs to a workspace (tenant). A valid session token
# (Authorization: Bearer ...) selects that account's workspace; anything else
# lands in the public "demo" workspace, so the demo keeps working openly
# while real dealership data stays private behind login.
_tenant_var: ContextVar[str] = ContextVar("tenant_slug", default="demo")


class _TenantMiddleware:
    """Pure ASGI middleware: resolve the tenant BEFORE routing so the
    contextvar is visible inside every endpoint (BaseHTTPMiddleware would
    run the endpoint in a copied context)."""

    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        slug = "demo"
        if scope.get("type") == "http":
            try:
                headers = dict(scope.get("headers") or [])
                auth = headers.get(b"authorization", b"").decode("latin1")
                if auth.lower().startswith("bearer "):
                    acct = accounts.account_for_token(auth[7:].strip())
                    if acct:
                        slug = acct["workspace_slug"]
            except Exception:
                slug = "demo"
            _tenant_var.set(slug)
        await self.app(scope, receive, send)
        # Persist the tenant's workspace graph after any write request, so
        # tenant data survives cold starts when a store is configured.
        if (scope.get("type") == "http" and slug != "demo"
                and scope.get("method") not in ("GET", "HEAD", "OPTIONS")):
            try:
                G.persist_tenant_graph(slug)
            except Exception:
                pass


app.add_middleware(_TenantMiddleware)


class ChatIn(BaseModel):
    phone: str
    name: str = ""
    message: str
    lang: str = "en"  # "en" | "ur" (Roman Urdu)


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
    _, mode = G.get_tenant_graph(_tenant_var.get())
    out = {
        "falkordb_mode": mode,  # embedded | external | memory (fallback)
        "graph": G.GRAPH_NAME,
        "workspace": _tenant_var.get(),
        "saas": {
            "tenancy": True,
            "persistence": accounts.persistence_mode(),
            "store": accounts.store_name(),
        },
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


def _current_slug():
    return _tenant_var.get()


def _need_graph():
    """Return the CURRENT workspace's graph (never crashes — falls back to memory)."""
    graph, _ = G.get_tenant_graph(_current_slug())
    return graph


def _dealer_brand():
    slug = _current_slug()
    if slug == "demo":
        return "Velocity Motors"
    ws = accounts.workspace_for_slug(slug)
    return (ws or {}).get("name") or "Velocity Motors"


@app.post("/api/chat")
def chat(body: ChatIn):
    graph = _need_graph()
    lang = (body.lang or "en").strip().lower()
    if lang not in ("en", "ur"):
        lang = "en"
    result = agents.handle_message(graph, body.phone.strip(),
                                   body.name.strip(), body.message.strip(),
                                   lang=lang)
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
    if _current_slug() != "demo":
        return JSONResponse({"ok": False, "error": "reseed is demo-workspace only"},
                            status_code=403)
    from app import seed as seedmod  # lazy: only needed for manual reseeds
    seedmod.seed()
    return {"ok": True, "note": "reseeded with FICTIONAL demo data"}


# ---------------------------------------------------------------------------
# SaaS — accounts, login, workspace info
# ---------------------------------------------------------------------------

class SignupIn(BaseModel):
    dealership_name: str
    owner_name: str = ""
    email: str
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


def _bearer(request: Request):
    auth = request.headers.get("authorization") or ""
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


@app.post("/api/auth/signup")
def signup(body: SignupIn):
    res, err = accounts.create_account(body.owner_name, body.email,
                                       body.password, body.dealership_name)
    if err:
        code = 409 if "already exists" in err else 400
        return JSONResponse({"ok": False, "error": err}, status_code=code)
    # Starter kit for the new workspace: owner as staff + standard policies.
    # NO fictional demo data — the dealership adds its own real data.
    try:
        from app import seed as seedmod
        graph, mode = G.get_tenant_graph(res["workspace"]["slug"])
        seedmod.seed_workspace(graph, mode, res["workspace"]["name"],
                               body.owner_name or "Owner")
    except Exception:
        pass  # workspace exists; starter kit is best-effort
    return {"ok": True, **res}


@app.post("/api/auth/login")
def login(body: LoginIn):
    res = accounts.authenticate(body.email, body.password)
    if not res:
        return JSONResponse({"ok": False, "error": "wrong email or password"},
                            status_code=401)
    return {"ok": True, **res}


@app.get("/api/auth/me")
def me(request: Request):
    acct = accounts.account_for_token(_bearer(request))
    if not acct:
        return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
    return {"ok": True, "account": acct, "workspace": acct["workspace"]}


@app.post("/api/auth/logout")
def logout(request: Request):
    accounts.logout(_bearer(request))
    return {"ok": True}


@app.get("/api/workspace")
def workspace_info():
    slug = _current_slug()
    graph = _need_graph()
    counts = {}
    try:
        if G._is_memory(graph):
            counts = graph.stats()
        else:
            res = graph.query("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS c")
            counts = {r[0]: r[1] for r in res.result_set}
    except Exception:
        counts = {}
    if slug == "demo":
        ws = {"slug": "demo", "name": "Velocity Motors (Demo)",
              "plan": "demo", "trial_days_left": None}
    else:
        ws = accounts.workspace_for_slug(slug) or {"slug": slug, "name": slug,
                                                    "plan": "trial"}
    return {"ok": True, "workspace": ws, "counts": counts,
            "onboarding": {
                "has_vehicles": counts.get("Vehicle", 0) > 0,
                "has_customers": counts.get("Customer", 0) > 0,
                "has_staff": counts.get("Staff", 0) > 0,
            }}


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
# Promise Watch — agent sweep: promise -> deadline -> reminder -> escalation -> log
# ---------------------------------------------------------------------------

@app.get("/api/promise/tracking")
def promise_tracking():
    """Read-only: classify all promises (overdue / due soon / ok / done). No side effects."""
    graph = _need_graph()
    return JSONResponse(agents.promise_watch(graph, dry_run=True))


@app.post("/api/promise/watch")
def run_promise_watch():
    """Run the Promise Watch agent: log reminders for due-soon promises,
    escalate overdue promises (webhook when configured), record every step
    in the Alert Log. One reminder/escalation per promise — never duplicates."""
    graph = _need_graph()
    return JSONResponse(agents.promise_watch(graph, dry_run=False))


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


# ---------------------------------------------------------------------------
# Neighborhood graph (vis.js) — advisor feature #1
# ---------------------------------------------------------------------------

@app.get("/api/graph/neighborhood")
def graph_neighborhood(phone: str):
    graph = _need_graph()
    return JSONResponse(G.neighborhood(graph, phone.strip()))


# ---------------------------------------------------------------------------
# Smart Match + FollowUp agent — advisor feature #4
# ---------------------------------------------------------------------------

class VehicleIn(BaseModel):
    stock_no: str
    make: str = ""
    model: str = ""
    year: int = 2024
    price: float = 0
    colour: str = ""
    body_type: str = ""


class SmartMatchIn(BaseModel):
    stock_no: str


@app.get("/api/vehicles")
def get_vehicles():
    graph = _need_graph()
    return {"vehicles": G.list_vehicles(graph)}


@app.post("/api/vehicles")
def add_vehicle(body: VehicleIn):
    graph = _need_graph()
    stock_no = body.stock_no.strip()
    if not stock_no:
        return JSONResponse({"ok": False, "error": "stock_no required"}, status_code=400)
    G.add_vehicle(graph, stock_no, body.make.strip(), body.model.strip(),
                  body.year, body.price, body.colour.strip(),
                  body.body_type.strip() or "sedan")
    return {"ok": True, "stock_no": stock_no}


# Shared LLM time budget for smart-match drafting: all follow-up drafts
# together must fit in this window; drafts that would exceed it fall back
# to the instant template so the endpoint never outlasts serverless limits.
_SMART_MATCH_LLM_BUDGET_S = 8.0


def _draft_followup(name, phone, vehicle, reasons, provider_label_out=None, deadline=None):
    """Draft a personalized WhatsApp-style follow-up. LLM first, template fallback.

    deadline: monotonic timestamp — skip the LLM call entirely when the
    shared budget is already spent.
    """
    v_desc = f"{vehicle.get('year')} {vehicle.get('make')} {vehicle.get('model')}"
    price = vehicle.get("price") or 0
    brand = _dealer_brand()
    template = (
        f"Hi {name}! A new {v_desc} ({vehicle.get('colour')}) just arrived at "
        f"PKR {price:,.0f} — it matches your interest and budget. "
        f"Want a free 30-minute test drive this week? — {brand}"
    )
    try:
        from app import free_llm
        if (deadline is None or time.monotonic() < deadline) and \
                free_llm.available_provider() is not None:
            system = ("You are a car dealership sales assistant. Write a short, warm, "
                      "personalized WhatsApp follow-up message (max 3 sentences) for a "
                      "customer about a newly arrived vehicle. Use ONLY the given facts. "
                      f"No invented prices. Sign off as '— {brand}'.")
            user = (f"Customer: {name} ({phone}). Reasons this car fits: "
                    f"{'; '.join(reasons)}. Vehicle: {v_desc}, colour {vehicle.get('colour')}, "
                    f"body {vehicle.get('body_type')}, price PKR {price:,.0f}.")
            text, label = free_llm.chat(system, user, max_tokens=200, temperature=0.5)
            if text and text.strip():
                return text.strip(), label
    except Exception:
        pass
    return template, "template"


@app.post("/api/smart-match")
def smart_match(body: SmartMatchIn):
    graph = _need_graph()
    res = G.smart_match(graph, body.stock_no.strip())
    if res.get("error"):
        return JSONResponse({"ok": False, "error": res["error"]}, status_code=404)
    vehicle = res["vehicle"]
    out_matches = []
    deadline = time.monotonic() + _SMART_MATCH_LLM_BUDGET_S
    for m in res["matches"]:
        msg, provider = _draft_followup(m["name"], m["phone"], vehicle, m["reasons"], deadline=deadline)
        out_matches.append({**m, "draft_message": msg, "draft_provider": provider})
    return {"ok": True, "vehicle": vehicle, "matches": out_matches}


# ---------------------------------------------------------------------------
# Staff leaderboard — advisor extra feature
# ---------------------------------------------------------------------------

@app.get("/api/staff/leaderboard")
def staff_leaderboard():
    graph = _need_graph()
    return JSONResponse(G.staff_leaderboard(graph))


# ---------------------------------------------------------------------------
# Trade-in valuation — "what will you give me for my old car?"
# ---------------------------------------------------------------------------

class TradeInIn(BaseModel):
    make: str = ""
    model: str = ""
    year: int = 2020
    mileage_km: int = 60000
    condition: str = "good"  # excellent | good | fair | poor


@app.post("/api/tradein/valuate")
def tradein_valuate(body: TradeInIn):
    return JSONResponse(G.valuate_tradein(body.make, body.model, body.year,
                                          body.mileage_km, body.condition))


# ---------------------------------------------------------------------------
# Test-drive booking
# ---------------------------------------------------------------------------

class TestDriveIn(BaseModel):
    customer_phone: str
    customer_name: str = ""
    stock_no: str = ""
    date: str = ""        # YYYY-MM-DD
    time_slot: str = ""   # one of G.TESTDRIVE_SLOTS


@app.get("/api/testdrive/slots")
def testdrive_slots():
    return {"slots": G.TESTDRIVE_SLOTS}


@app.post("/api/testdrive/book")
def testdrive_book(body: TestDriveIn):
    graph = _need_graph()
    res = G.book_testdrive(graph, body.customer_phone.strip(),
                           body.customer_name.strip() or body.customer_phone.strip(),
                           body.stock_no.strip(), body.date.strip(),
                           body.time_slot.strip())
    if not res.get("ok"):
        return JSONResponse(res, status_code=409)
    return JSONResponse(res)


@app.get("/api/testdrive/list")
def testdrive_list():
    graph = _need_graph()
    return {"bookings": G.list_testdrives(graph)}


# ---------------------------------------------------------------------------
# Financing pre-check (EMI)
# ---------------------------------------------------------------------------

class FinanceIn(BaseModel):
    price: float = 3000000
    down_payment: float = 600000
    months: int = 36
    annual_rate_pct: float = 22.0


@app.post("/api/finance/quote")
def finance_quote(body: FinanceIn):
    return JSONResponse(G.finance_quote(body.price, body.down_payment,
                                        body.months, body.annual_rate_pct))


# ---------------------------------------------------------------------------
# Service reminders — "when is my car's service due?"
# ---------------------------------------------------------------------------

@app.get("/api/service/due")
def service_due():
    graph = _need_graph()
    return JSONResponse(G.service_due(graph))


# ---------------------------------------------------------------------------
# "Brain remembers" — full customer memory card
# ---------------------------------------------------------------------------

@app.get("/api/memory/{phone}")
def customer_memory(phone: str):
    graph = _need_graph()
    res = G.customer_memory(graph, phone.strip())
    if res.get("error"):
        return JSONResponse(res, status_code=404)
    return JSONResponse(res)


# ---------------------------------------------------------------------------
# n8n webhook alerts for broken promises / missed callbacks
# ---------------------------------------------------------------------------

@app.post("/api/promises/{pr_id}/alert")
def promise_alert(pr_id: str):
    graph = _need_graph()
    res = G.send_promise_alert(graph, pr_id)
    if not res.get("ok"):
        return JSONResponse(res, status_code=404 if "not found" in str(res.get("error", "")) else 502)
    return JSONResponse(res)


@app.post("/api/callbacks/{cb_id}/alert")
def callback_alert(cb_id: str):
    graph = _need_graph()
    res = G.send_callback_alert(graph, cb_id)
    if not res.get("ok"):
        return JSONResponse(res, status_code=404 if "not found" in str(res.get("error", "")) else 502)
    return JSONResponse(res)


# ---------------------------------------------------------------------------
# Alert Log — audit trail of every escalation attempt (newest first)
# ---------------------------------------------------------------------------

@app.get("/api/alerts")
def get_alerts(limit: int = 50):
    graph = _need_graph()
    return {"alerts": G.list_alerts(graph, max(1, min(int(limit), 200)))}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8125")))
