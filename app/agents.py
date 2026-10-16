"""Multi-agent orchestrator: Triage -> Sales / Service specialists.

All agents share state through FalkorDB (Track 2: memory + coordination).
The graph is CENTRAL: every decision reads from it, every turn writes to it.
Agents ACT on connected data (Track 1): churn-risk escalation, ticket creation.
Together they form the dealership's Company Brain (Track 3).
"""
from app import graph as G
from app import llm


USD_TO_PKR = 280  # fixed demo rate for $-denominated budget hints


def _budget_from_text(text: str) -> int | None:
    """Parse budget hints like '$25k', '$25000', '25k', '30 lakh', '2 million'.

    $-prefixed amounts are USD and convert to PKR at USD_TO_PKR. A bare 'k'
    suffix means thousand; without a '$' the amount is read as PKR.
    Returns None when nothing usable is found (the caller then falls back).
    """
    import re
    t = text.lower()
    m = re.search(r"\$\s*(\d[\d,]*)\s*(k)?", t)
    if m:
        val = int(m.group(1).replace(",", ""))
        if m.group(2):
            val *= 1_000
        return int(val * USD_TO_PKR)
    m = re.search(r"(\d[\d,]*)\s*(k|lakh|lac|million)?", t)
    if not m:
        return None
    val = int(m.group(1).replace(",", ""))
    unit = m.group(2)
    if unit == "k":
        val *= 1_000
    elif unit in ("lakh", "lac"):
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


# Active sales filters persist across turns for each phone (in-memory, demo
# session). Filters must never loosen silently: a follow-up without new
# filters re-uses the last ones, and alternatives are always labeled.
_LAST_FILTERS: dict[str, dict] = {}


def sales_agent(graph, phone: str, name: str, message: str, customer: dict | None,
                returning: bool = False, lang: str = "en") -> AgentResult:
    budget = _budget_from_text(message)
    budget_from_text = budget is not None
    if budget is None:
        budget = (customer or {}).get("budget") or 5_000_000
    body_type, colour = parse_vehicle_filters(message)
    # Carry forward filters the customer stated earlier (never loosen silently).
    last = _LAST_FILTERS.get(phone) or {}
    carried = {k: v for k, v in last.items() if v}
    if not body_type:
        body_type = last.get("body_type")
    if not colour:
        colour = last.get("colour")
    _LAST_FILTERS[phone] = {"body_type": body_type, "colour": colour}
    vehicles = G.vehicles_in_budget(graph, budget, body_type=body_type, colour=colour)
    history = G.customer_history(graph, phone)
    desc = _describe_filters(body_type, colour)
    no_match = bool(desc and not vehicles)
    alternatives = []
    if no_match:
        # Loosen ONLY the body-type filter for alternatives — still budget-bound,
        # and always labeled as alternatives, never presented as matches.
        alternatives = G.vehicles_in_budget(graph, budget, colour=colour)[:3]
    context = {
        "message": message,
        "customer": customer or {"name": name, "phone": phone},
        "vehicles": vehicles,
        "history": history,
        "returning": returning,
        "filters": {"body_type": body_type, "colour": colour, "budget": budget},
        "alternatives": alternatives,
        # Structured no-match flag: heuristic builds a clean message;
        # the LLM path adds a "say it honestly, then label alternatives" rule.
        "no_match": {"desc": desc, "budget": budget} if no_match else None,
    }
    reply, provider = llm.draft_reply("sales", context, lang)
    # Echo the interpreted budget so silent fallbacks stop looking arbitrary.
    if budget_from_text:
        reply += ("\n\nMe ne aapka budget PKR {:,} samjha hai — agar ye ghalat hai to bata dein.".format(budget)
                  if lang == "ur"
                  else "\n\nI'm using a budget of PKR {:,} — tell me if that's off.".format(budget))
    if no_match:
        actions = [f"no {desc} vehicles found within budget PKR {budget:,} (honest no-match)"]
        if alternatives:
            actions.append(f"showed {len(alternatives)} labeled alternatives (body-type filter loosened)")
    else:
        actions = [f"recommended {len(vehicles)} vehicles within budget PKR {budget:,}" +
                   (f" matching {desc}" if desc else "")]
    return AgentResult(agent="sales", reply=reply, provider=provider,
                       context_used={"vehicles": len(vehicles),
                                     "past_conversations": len(history["conversations"]),
                                     "filters": desc or "none",
                                     "filters_carried": carried},
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


def finance_agent(graph, phone: str, name: str, message: str, customer: dict | None,
                   returning: bool = False, lang: str = "en") -> AgentResult:
    """Financing answers: EMI quote on the customer's budget using the same
    finance-quote logic as the /api/finance/quote calculator (22% p.a.
    reducing balance, 36 months, 20% down payment). Deterministic template —
    money math never goes through the LLM."""
    budget = (customer or {}).get("budget") or _budget_from_text(message) or 5_000_000
    price = float(budget)
    down_payment = round(price * 0.20)
    months, rate = 36, 22.0
    q = G.finance_quote(price, down_payment, months, rate)
    if lang == "ur":
        reply = (
            f"Assalam-o-Alaikum {name}! PKR {q['price']:,} ki gari ke liye financing estimate:\n\n"
            f"- Down payment (20%): PKR {q['down_payment']:,}\n"
            f"- Finance honay wali raqam: PKR {q['financed']:,}\n"
            f"- Muddat: {q['months']} mahine @ {q['annual_rate_pct']}% saalana (reducing balance)\n"
            f"- **Monthly EMI: PKR {q['emi']:,}**\n"
            f"- Kul adaigi: PKR {q['total_payable']:,} (kul sood PKR {q['total_interest']:,})\n\n"
            f"{q['note']}")
    else:
        reply = (
            f"Here is a financing estimate for a car at PKR {q['price']:,}:\n\n"
            f"- Down payment (20%): PKR {q['down_payment']:,}\n"
            f"- Amount financed: PKR {q['financed']:,}\n"
            f"- Term: {q['months']} months @ {q['annual_rate_pct']}% p.a. reducing balance\n"
            f"- **Monthly EMI: PKR {q['emi']:,}**\n"
            f"- Total payable: PKR {q['total_payable']:,} (total interest PKR {q['total_interest']:,})\n\n"
            f"{q['note']}")
    return AgentResult(
        agent="finance", reply=reply, provider="heuristic",
        context_used={"budget": budget, "quote": f"EMI PKR {q['emi']:,}/mo x {q['months']} mo"},
        actions=[f"quoted finance: EMI PKR {q['emi']:,} x {q['months']} months on PKR {q['price']:,}"])


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
    elif intent == "finance":
        res = finance_agent(graph, phone, display_name, message, customer, returning=returning, lang=lang)
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


# ---------------------------------------------------------------------------
# Promise Watch agent — promise -> deadline -> reminder -> escalation -> log
#
# A sweep agent (Graph Hacks Day-1): every run classifies all open promises
# against their deadlines, generates reminders for promises due soon, escalates
# overdue promises exactly like a churn-risk case (staff WhatsApp message +
# n8n webhook when configured), and writes EVERY step into the Alert Log
# delivery trail. Never double-escalates: the log itself is the dedupe source.
# ---------------------------------------------------------------------------

DUE_SOON_MS = 24 * 3600 * 1000  # a promise "due soon" when < 24h remain


def _fmt_dur(ms: int | None) -> str:
    ms = max(0, int(ms or 0))
    d, h = divmod(ms // 60000, 24 * 60)
    if d:
        return f"{d}d {h // 60}h"
    return f"{h // 60}h {(h % 60)}m"


def _already_logged(graph, event: str, ref_id: str) -> bool:
    """The Alert Log is the dedupe source: one reminder / escalation per promise."""
    try:
        alerts = G.list_alerts(graph, 200)
    except Exception:
        return False
    return any(a.get("event") == event and a.get("ref_id") == ref_id for a in alerts)


def _reminder_message(p: dict) -> str:
    when = ("overdue by " + _fmt_dur(p.get("overdue_ms")) if p.get("overdue")
            else "due in " + _fmt_dur(p.get("due_in_ms")))
    return (f"Hi {p.get('staff')}, this is Dealership Brain. Your promise to "
            f"{p.get('customer_name')} — “{p.get('promise')}” — is {when}. "
            f"Please update {p.get('customer_phone')} today. — Velocity Motors")


def promise_watch(graph, dry_run: bool = False) -> dict:
    """One sweep of the promise-completion workflow.

    dry_run=True  -> classify only, no side effects (read-only tracking view).
    dry_run=False -> also log reminders + fire escalations for newly-flagged items.
    Returns the full plan plus every action taken (or that WOULD be taken).
    """
    promises = G.list_promises(graph)
    plan = {"overdue": [], "due_soon": [], "ok": [], "done": []}
    for p in promises:
        if p.get("status") == "done":
            plan["done"].append(p["id"])
        elif p.get("overdue"):
            plan["overdue"].append(p)
        elif (p.get("due_in_ms") or 0) <= DUE_SOON_MS:
            plan["due_soon"].append(p)
        else:
            plan["ok"].append(p["id"])

    actions = {"reminded": [], "escalated": [], "skipped": []}
    if not dry_run:
        # Reminders: due soon -> log a reminder entry (no webhook; internal).
        for p in plan["due_soon"]:
            if _already_logged(graph, "promise_reminder", p["id"]):
                actions["skipped"].append({"id": p["id"], "reason": "reminder already logged"})
                continue
            msg = _reminder_message(p)
            G.record_alert(graph, "promise_reminder", p["id"],
                           p.get("customer_name"), p.get("customer_phone"),
                           p.get("staff"),
                           f"Promise due in {_fmt_dur(p.get('due_in_ms'))}: “{p.get('promise')}”",
                           "reminder_logged", 0, msg)
            actions["reminded"].append({"id": p["id"], "customer": p.get("customer_name"),
                                       "promise": p.get("promise"), "message": msg})
        # Escalations: overdue -> churn-risk style escalation (webhook when
        # configured, ALWAYS recorded in the delivery log).
        for p in plan["overdue"]:
            if _already_logged(graph, "promise_overdue", p["id"]):
                actions["skipped"].append({"id": p["id"], "reason": "escalation already logged"})
                continue
            res = G.send_promise_alert(graph, p["id"])
            actions["escalated"].append({"id": p["id"], "customer": p.get("customer_name"),
                                        "promise": p.get("promise"),
                                        "sent": bool(res.get("sent")),
                                        "ok": bool(res.get("ok"))})

    return {
        "dry_run": dry_run,
        "counts": {k: len(v) for k, v in plan.items()},
        "overdue": [
            {"id": p["id"], "customer_name": p.get("customer_name"),
             "customer_phone": p.get("customer_phone"), "staff": p.get("staff"),
             "promise": p.get("promise"), "deadline_at": p.get("deadline_at"),
             "overdue_ms": p.get("overdue_ms")}
            for p in plan["overdue"]],
        "due_soon": [
            {"id": p["id"], "customer_name": p.get("customer_name"),
             "customer_phone": p.get("customer_phone"), "staff": p.get("staff"),
             "promise": p.get("promise"), "deadline_at": p.get("deadline_at"),
             "due_in_ms": p.get("due_in_ms")}
            for p in plan["due_soon"]],
        "ok_count": len(plan["ok"]),
        "done_count": len(plan["done"]),
        "actions": actions,
        "note": "Velocity Motors demo data is fictional (app/seed.py).",
    }
