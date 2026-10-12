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


_BODY_TYPES = ["suv", "sedan", "hatchback", "coupe", "pickup", "van", "crossover"]
_COLOURS = ["black", "white", "silver", "grey", "gray", "red", "blue",
            "green", "beige", "brown", "gold", "bronze"]


def parse_vehicle_filters(text: str) -> tuple[str | None, str | None]:
    """Extract (body_type, colour) from a customer message. Returns (None, None) if absent."""
    import re
    t = text.lower()
    body_type = None
    for bt in _BODY_TYPES:
        if re.search(rf"\b{bt}s?\b", t):
            body_type = "suv" if bt == "suv" else bt
            break
    colour = None
    for c in _COLOURS:
        if re.search(rf"\b{c}\b", t):
            colour = "grey" if c == "gray" else c
            break
    return body_type, colour


def _describe_filters(body_type, colour) -> str:
    bits = []
    if colour:
        bits.append(colour)
    if body_type:
        bits.append(body_type.upper())
    return " ".join(bits) if bits else ""


class AgentResult(dict):
    pass


def triage(message: str) -> tuple[str, str]:
    """Route to the right specialist. Returns (intent, provider)."""
    return llm.classify_intent(message)


def sales_agent(graph, phone: str, name: str, message: str, customer: dict | None,
                returning: bool = False, lang: str = "en") -> AgentResult:
    budget = (customer or {}).get("budget") or _budget_from_text(message) or 5_000_000
    body_type, colour = parse_vehicle_filters(message)
    vehicles = G.vehicles_in_budget(graph, budget, body_type=body_type, colour=colour)
    history = G.customer_history(graph, phone)
    desc = _describe_filters(body_type, colour)
    no_match = bool(desc and not vehicles)
    context = {
        "message": message,
        "customer": customer or {"name": name, "phone": phone},
        "vehicles": vehicles,
        "history": history,
        "returning": returning,
        "filters": {"body_type": body_type, "colour": colour, "budget": budget},
        # Structured no-match flag: heuristic builds a clean message;
        # the LLM path adds a "do NOT list unrelated vehicles" instruction.
        "no_match": {"desc": desc, "budget": budget} if no_match else None,
    }
    reply, provider = llm.draft_reply("sales", context, lang)
    if no_match:
        actions = [f"no {desc} vehicles found within budget PKR {budget:,} (honest no-match)"]
    else:
        actions = [f"recommended {len(vehicles)} vehicles within budget PKR {budget:,}" +
                   (f" matching {desc}" if desc else "")]
    return AgentResult(agent="sales", reply=reply, provider=provider,
                       context_used={"vehicles": len(vehicles),
                                     "past_conversations": len(history["conversations"]),
                                     "filters": desc or "none"},
                       actions=actions)


def service_agent(graph, phone: str, name: str, message: str, customer: dict | None,
                   returning: bool = False, lang: str = "en") -> AgentResult:
    history = G.customer_history(graph, phone)
    policies = G.find_policy(graph, "warranty")
    # ACT on connected data: high-priority open ticket -> escalate
    open_high = [t for t in history["tickets"]
                 if t["status"] == "open" and t["priority"] == "high"]
    context = {"message": message,
               "customer": customer or {"name": name, "phone": phone},
               "history": history, "policies": policies, "returning": returning}
    reply, provider = llm.draft_reply("service", context, lang)
    actions = []
    if open_high:
        actions.append(f"ESCALATED ticket {open_high[0]['id']} to service manager (high-priority open)")
        if lang == "ur":
            reply += f"\n\nMene aapki open ticket {open_high[0]['id']} ko service manager ke paas urgent mark kar diya hai."
        else:
            reply += f"\n\nI've also flagged your open ticket {open_high[0]['id']} as urgent with our service manager."
    elif "book" in message.lower() or "appointment" in message.lower():
        tid = G.create_ticket(graph, phone, message[:120], priority="normal")
        actions.append(f"created service ticket {tid}")
        if lang == "ur":
            reply += f"\n\nMene aapke liye service ticket {tid} bana di hai."
        else:
            reply += f"\n\nI've created service ticket {tid} for you."
    return AgentResult(agent="service", reply=reply, provider=provider,
                       context_used={"past_tickets": len(history["tickets"])},
                       actions=actions)


def policy_agent(graph, phone: str, name: str, message: str, customer: dict | None,
                  returning: bool = False, lang: str = "en") -> AgentResult:
    import re
    kw = "warranty"
    for w in ("return", "exchange", "financ", "test drive", "warranty"):
        if w in message.lower():
            kw = w
            break
    policies = G.find_policy(graph, kw)
    context = {"message": message,
               "customer": customer or {"name": name, "phone": phone},
               "policies": policies, "returning": returning}
    reply, provider = llm.draft_reply("policy", context, lang)
    return AgentResult(agent="policy", reply=reply, provider=provider,
                       context_used={"policies": len(policies)}, actions=[])


def general_agent(graph, phone: str, name: str, message: str, customer: dict | None,
                   returning: bool = False, lang: str = "en") -> AgentResult:
    history = G.customer_history(graph, phone)
    context = {"message": message,
               "customer": customer or {"name": name, "phone": phone},
               "history": history, "returning": returning}
    reply, provider = llm.draft_reply("general", context, lang)
    return AgentResult(agent="general", reply=reply, provider=provider,
                       context_used={"past_conversations": len(history["conversations"])},
                       actions=[])


def handle_message(graph, phone: str, name: str, message: str,
                   lang: str = "en") -> dict:
    """Full loop: triage -> specialist -> write memory back to graph."""
    customer = G.find_customer_by_phone(graph, phone)
    display_name = (customer or {}).get("name") or name or "there"

    # Honest memory: "returning" ONLY when this phone has prior conversations.
    # Never claim to remember a brand-new customer (trust-breaking bug fix).
    prior = G.customer_history(graph, phone)
    returning = bool(prior["conversations"])

    intent, triage_provider = triage(message)

    if intent == "sales":
        res = sales_agent(graph, phone, display_name, message, customer, returning=returning, lang=lang)
    elif intent == "service":
        res = service_agent(graph, phone, display_name, message, customer, returning=returning, lang=lang)
    elif intent == "policy":
        res = policy_agent(graph, phone, display_name, message, customer, returning=returning, lang=lang)
    else:
        res = general_agent(graph, phone, display_name, message, customer, returning=returning, lang=lang)

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
        "returning_customer": returning,
        "lang": lang,
    }


def churn_report(graph) -> dict:
    """Manager view: who might churn? Agent acts on connected data (Track 1)."""
    at_risk = G.churn_risk_customers(graph)
    return {"at_risk": at_risk,
            "count": len(at_risk),
            "note": "customers with unresolved HIGH-priority tickets"}
