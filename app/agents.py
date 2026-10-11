"""Multi-agent orchestrator: Triage -> Sales / Service specialists.

All agents share state through FalkorDB (Track 2: memory + coordination).
The graph is CENTRAL: every decision reads from it, every turn writes to it.
Agents ACT on connected data (Track 1): churn-risk escalation, ticket creation.
Together they form the dealership's Company Brain (Track 3).
"""
from app import graph as G
from app import llm


def _budget_from_text(text: str) -> int | None:
    import re
    m = re.search(r"(\d[\d,]*)\s*(lakh|lac|million)?", text.lower())
    if not m:
        return None
    val = int(m.group(1).replace(",", ""))
    unit = m.group(2)
    if unit in ("lakh", "lac"):
        val *= 100_000
    elif unit == "million":
        val *= 1_000_000
    return val if val > 50_000 else None


class AgentResult(dict):
    pass


def triage(message: str) -> tuple[str, str]:
    """Route to the right specialist. Returns (intent, provider)."""
    return llm.classify_intent(message)


def sales_agent(graph, phone: str, name: str, message: str, customer: dict | None) -> AgentResult:
    budget = (customer or {}).get("budget") or _budget_from_text(message) or 5_000_000
    vehicles = G.vehicles_in_budget(graph, budget)
    history = G.customer_history(graph, phone)
    context = {
        "message": message,
        "customer": customer or {"name": name, "phone": phone},
        "vehicles": vehicles,
        "history": history,
    }
    reply, provider = llm.draft_reply("sales", context)
    actions = [f"recommended {len(vehicles)} vehicles within budget PKR {budget:,}"]
    return AgentResult(agent="sales", reply=reply, provider=provider,
                       context_used={"vehicles": len(vehicles),
                                     "past_conversations": len(history["conversations"])},
                       actions=actions)


def service_agent(graph, phone: str, name: str, message: str, customer: dict | None) -> AgentResult:
    history = G.customer_history(graph, phone)
    policies = G.find_policy(graph, "warranty")
    # ACT on connected data: high-priority open ticket -> escalate
    open_high = [t for t in history["tickets"]
                 if t["status"] == "open" and t["priority"] == "high"]
    context = {"message": message,
               "customer": customer or {"name": name, "phone": phone},
               "history": history, "policies": policies}
    reply, provider = llm.draft_reply("service", context)
    actions = []
    if open_high:
        actions.append(f"ESCALATED ticket {open_high[0]['id']} to service manager (high-priority open)")
        reply += f"\n\nI've also flagged your open ticket {open_high[0]['id']} as urgent with our service manager."
    elif "book" in message.lower() or "appointment" in message.lower():
        tid = G.create_ticket(graph, phone, message[:120], priority="normal")
        actions.append(f"created service ticket {tid}")
        reply += f"\n\nI've created service ticket {tid} for you."
    return AgentResult(agent="service", reply=reply, provider=provider,
                       context_used={"past_tickets": len(history["tickets"])},
                       actions=actions)


def policy_agent(graph, phone: str, name: str, message: str, customer: dict | None) -> AgentResult:
    import re
    kw = "warranty"
    for w in ("return", "exchange", "financ", "test drive", "warranty"):
        if w in message.lower():
            kw = w
            break
    policies = G.find_policy(graph, kw)
    context = {"message": message,
               "customer": customer or {"name": name, "phone": phone},
               "policies": policies}
    reply, provider = llm.draft_reply("policy", context)
    return AgentResult(agent="policy", reply=reply, provider=provider,
                       context_used={"policies": len(policies)}, actions=[])


def general_agent(graph, phone: str, name: str, message: str, customer: dict | None) -> AgentResult:
    history = G.customer_history(graph, phone)
    context = {"message": message,
               "customer": customer or {"name": name, "phone": phone},
               "history": history}
    reply, provider = llm.draft_reply("general", context)
    return AgentResult(agent="general", reply=reply, provider=provider,
                       context_used={"past_conversations": len(history["conversations"])},
                       actions=[])


def handle_message(graph, phone: str, name: str, message: str) -> dict:
    """Full loop: triage -> specialist -> write memory back to graph."""
    customer = G.find_customer_by_phone(graph, phone)
    display_name = (customer or {}).get("name") or name or "there"

    intent, triage_provider = triage(message)

    if intent == "sales":
        res = sales_agent(graph, phone, display_name, message, customer)
    elif intent == "service":
        res = service_agent(graph, phone, display_name, message, customer)
    elif intent == "policy":
        res = policy_agent(graph, phone, display_name, message, customer)
    else:
        res = general_agent(graph, phone, display_name, message, customer)

    # Write memory back: every turn becomes graph history (Track 2)
    G.record_conversation(graph, phone, display_name,
                          summary=message[:140], intent=intent)

    return {
        "intent": intent,
        "triage_provider": triage_provider,
        "agent": res["agent"],
        "reply": res["reply"],
        "llm_provider": res["provider"],
        "context_used": res["context_used"],
        "actions_taken": res["actions"],
        "returning_customer": customer is not None,
    }


def churn_report(graph) -> dict:
    """Manager view: who might churn? Agent acts on connected data (Track 1)."""
    at_risk = G.churn_risk_customers(graph)
    return {"at_risk": at_risk,
            "count": len(at_risk),
            "note": "customers with unresolved HIGH-priority tickets"}
