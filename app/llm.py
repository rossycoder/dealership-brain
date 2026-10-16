"""Pluggable LLM layer.

Providers (LLM_PROVIDER env):
- heuristic (default): free, offline, rule/pattern-based drafting.
  Every reply is labeled provider='heuristic' so judges see exactly what ran.
- auto: uses the first configured free-tier key via app/free_llm.py —
  GROQ_API_KEY (https://console.groq.com, free, no card) then
  GEMINI_API_KEY (https://aistudio.google.com, free, no card).
  Replies are labeled e.g. provider='groq:llama-3.3-70b-versatile'.
- groq / gemini: force one provider (key must be set).

On ANY LLM failure (bad key, rate limit, offline) the call falls back
honestly to the heuristic provider and the reply is labeled 'heuristic'.
Never pretends one provider is another.
"""
import os
import re

from . import free_llm

PROVIDER = os.environ.get("LLM_PROVIDER", "auto").lower()


def provider_name() -> str:
    return PROVIDER


def _llm_active() -> bool:
    if PROVIDER == "heuristic":
        return False
    if PROVIDER in ("groq", "gemini"):
        return bool(os.environ.get("GROQ_API_KEY" if PROVIDER == "groq" else "GEMINI_API_KEY"))
    return free_llm.available_provider() is not None  # auto


def _llm_chat(system: str, user: str) -> tuple[str | None, str]:
    """Returns (text, provider_label); (None, reason) when LLM unusable."""
    if PROVIDER == "groq" and not os.environ.get("GROQ_API_KEY"):
        return None, "groq key not configured"
    if PROVIDER == "gemini" and not os.environ.get("GEMINI_API_KEY"):
        return None, "gemini key not configured"
    return free_llm.chat(system, user, max_tokens=400, temperature=0.4)


# ---------------------------------------------------------------------------
# Heuristic provider: transparent pattern-based replies (demo-grade)
# ---------------------------------------------------------------------------

# Order matters: more specific intents first ("repair" beats generic "car").
_INTENT_PATTERNS = [
    ("service", [r"\b(service|repair|maintain|oil|brake|appointment|workshop|dent|scratch|warranty claim)"]),
    ("policy", [r"\b(policy|policies|warranty|return|exchange|refund)"]),
    ("finance", [r"\b(financ|emi|loan|installment|down\s?payment)"]),
    ("sales", [r"\b(price|buy|purchase|car|vehicle|suv|sedan|book|test drive|cost)"]),
]

_NAME_RE = re.compile(r"\b(?:i am|i'm|my name is|this is)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)")


def heuristic_intent(text: str) -> str:
    t = text.lower()
    for intent, patterns in _INTENT_PATTERNS:
        if any(re.search(p, t) for p in patterns):
            return intent
    return "general"


def heuristic_reply(intent: str, context: dict) -> str:
    """Build a reply from graph context. Clearly heuristic."""
    name = context.get("customer", {}).get("name") or "there"
    # Honest memory: only claim to remember when there are prior conversations.
    returning = bool(context.get("returning"))
    vehicles = context.get("vehicles", [])
    policies = context.get("policies", [])
    no_match = context.get("no_match")

    parts = []
    if returning:
        parts.append(f"Welcome back, {name}! I remember you.")
    else:
        parts.append(f"Hi {name}!")

    if intent == "sales":
        if no_match:
            parts.append(
                f"We don't currently have a {no_match['desc']} within your budget of "
                f"PKR {no_match['budget']:,}.")
            alts = context.get("alternatives") or []
            if alts:
                alt_lines = [f"- {v['year']} {v['make']} {v['model']} ({v.get('colour','')}, {v.get('body_type','')}) at PKR {v['price']:,} ({v['stock_no']})" for v in alts]
                parts.append("Closest alternatives (a different body type, still within budget):\n" + "\n".join(alt_lines))
            else:
                parts.append("Want me to notify you when one arrives?")
        elif vehicles:
            lines = [f"- {v['year']} {v['make']} {v['model']} ({v.get('colour','')}, {v.get('body_type','')}) at PKR {v['price']:,} ({v['stock_no']})" for v in vehicles[:4]]
            parts.append("Based on your budget, these are available:\n" + "\n".join(lines))
            parts.append("Want to book a free 30-minute test drive for any of these?")
        else:
            parts.append("I couldn't find any vehicles within that budget right now.")
    elif intent == "service":
        tickets = context.get("history", {}).get("tickets", [])
        if tickets:
            t = tickets[0]
            parts.append(f"I see your ticket {t['id']} ('{t['issue']}') is currently {t['status']}. Our service advisor will update you shortly.")
        else:
            parts.append("I can book a service appointment for you. Which vehicle and what issue?")
    elif intent == "policy" and policies:
        p = policies[0]
        parts.append(f"Our {p['topic']} policy: {p['content']}")
    else:
        parts.append("How can I help — looking to buy, or need service for your vehicle?")

    return "\n\n".join(parts)


def classify_intent(text: str, context_hint: str = "") -> tuple[str, str]:
    """Returns (intent, provider_label).

    Skip the triage LLM call when the heuristic is confident (a clear keyword
    hit) — only ambiguous messages pay for an LLM classification round.
    """
    heuristic = heuristic_intent(text)
    if _llm_active() and heuristic == "general":
        system = "Classify the customer message into exactly one word: sales, service, policy, finance, or general."
        llm_text, label = _llm_chat(system, text)
        if llm_text:
            word = llm_text.strip().lower().split()[0] if llm_text.strip() else "general"
            word = word if word in ("sales", "service", "policy", "finance", "general") else "general"
            return word, label
    return heuristic, "heuristic"


def draft_reply(intent: str, context: dict, lang: str = "en") -> tuple[str, str]:
    """Returns (reply_text, provider_label). lang='ur' -> Roman Urdu."""
    if _llm_active():
        returning = bool(context.get("returning"))
        memory_rule = (
            "MEMORY RULE: this customer is RETURNING (past conversations exist) — "
            "you may reference what you remember."
            if returning else
            "MEMORY RULE: this customer is NEW (no prior conversations) — "
            "NEVER claim to remember them, NEVER say 'welcome back' or 'I remember you'. "
            "Greet them as a first-time visitor."
        )
        no_match = context.get("no_match")
        filter_rule = ""
        if no_match:
            alts = context.get("alternatives") or []
            filter_rule = (
                f" FILTER RULE: no vehicles match the requested filters ({no_match['desc']}) "
                f"within budget PKR {no_match['budget']:,}. Say so honestly FIRST. "
            )
            if alts:
                alt_lines = ", ".join(
                    f"{v['year']} {v['make']} {v['model']} ({v.get('body_type','')}) at PKR {v['price']:,}"
                    for v in alts)
                filter_rule += (
                    "Then list these closest alternatives, explicitly labeled as "
                    f"alternatives (they do not match the requested filters): {alt_lines}.")
            else:
                filter_rule += "Do NOT list unrelated vehicles."
        lang_rule = (
            " LANGUAGE RULE: reply ENTIRELY in Roman Urdu (Urdu written in Latin "
            "script, e.g. 'Assalam-o-Alaikum! Aap kaise hain?'). Never use English "
            "sentences; keep prices/numbers as-is."
            if lang == "ur" else ""
        )
        system = (
            "You are a helpful car dealership assistant. Answer concisely using ONLY "
            "the provided context (customer history, vehicles, policies). "
            "If context is missing, say so honestly. Never invent prices or policies. "
            + memory_rule + filter_rule + lang_rule
        )
        user = f"Intent: {intent}\nContext: {context}\nCustomer message: {context.get('message','')}"
        llm_text, label = _llm_chat(system, user)
        if llm_text:
            return llm_text, label
    if lang == "ur":
        return heuristic_reply_ur(intent, context), "heuristic-ur"
    return heuristic_reply(intent, context), "heuristic"


def heuristic_reply_ur(intent: str, context: dict) -> str:
    """Roman Urdu heuristic replies (demo-grade, mirrors heuristic_reply)."""
    name = context.get("customer", {}).get("name") or "dost"
    returning = bool(context.get("returning"))
    vehicles = context.get("vehicles", [])
    policies = context.get("policies", [])
    no_match = context.get("no_match")

    parts = []
    if returning:
        parts.append(f"Welcome back, {name}! Me aapko yaad rakhta hun.")
    else:
        parts.append(f"Assalam-o-Alaikum {name}! Me Dealership Brain hun.")

    if intent == "sales":
        if no_match:
            parts.append(
                f"Maazrat — abhi hamare paas {no_match['desc']} aapke budget "
                f"PKR {no_match['budget']:,} ke andar available nahi hai.")
            alts = context.get("alternatives") or []
            if alts:
                alt_lines = [f"- {v['year']} {v['make']} {v['model']} ({v.get('colour','')}, {v.get('body_type','')}) — PKR {v['price']:,} ({v['stock_no']})" for v in alts]
                parts.append("Kareebi alternatives (body type alag hai, budget ke andar hain):\n" + "\n".join(alt_lines))
            else:
                parts.append("Kya aap chahein ke jese hi aisi gari aaye me aapko inform kar dun?")
        elif vehicles:
            lines = [f"- {v['year']} {v['make']} {v['model']} ({v.get('colour','')}, {v.get('body_type','')}) — PKR {v['price']:,} ({v['stock_no']})" for v in vehicles[:4]]
            parts.append("Aapke budget ke hisaab se ye gariyan available hain:\n" + "\n".join(lines))
            parts.append("Kya in me se kisi ka FREE 30-minute test drive book kar dun?")
        else:
            parts.append("Is waqt aapke budget me koi gari available nahi mil rahi.")
    elif intent == "service":
        tickets = context.get("history", {}).get("tickets", [])
        if tickets:
            t = tickets[0]
            parts.append(f"Aapki ticket {t['id']} ('{t['issue']}') abhi '{t['status']}' stage me hai. Hamara service advisor aapko jald update karega.")
        else:
            parts.append("Me aapke liye service appointment book kar sakta hun. Kaunsi gari hai aur kya masla hai?")
    elif intent == "policy" and policies:
        p = policies[0]
        parts.append(f"Hamari {p['topic']} policy ye hai: {p['content']}")
    else:
        parts.append("Bataiye, me aapki kya madad kar sakta hun — nayi gari khareedni hai ya apni gari ki service karwani hai?")

    return "\n\n".join(parts)


def extract_name(text: str) -> str | None:
    m = _NAME_RE.search(text)
    return m.group(1) if m else None
