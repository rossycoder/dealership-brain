"""free_llm.py — Real free-tier LLM chat for hackathon demos.

Auto-detects providers, in priority order:
  0. Pollinations.ai -> ZERO signup, ZERO API key. Works out of the box!
     https://text.pollinations.ai/openai (OpenAI-compatible). Free, no limits for demos.
  1. GROQ_API_KEY   -> Groq (OpenAI-compatible). Free tier, no credit card.
     Signup: https://console.groq.com (Google/GitHub login).
     Model: GROQ_MODEL, default "llama-3.3-70b-versatile".
  2. GEMINI_API_KEY -> Google AI Studio. Free tier, no credit card.
     Signup: https://aistudio.google.com ("Get API key").
     Model: GEMINI_MODEL, default "gemini-2.5-flash".

Stdlib only (urllib) — no new dependencies.

    from app import free_llm  # or: from . import free_llm / import free_llm
    text, label = free_llm.chat("You are helpful.", "Say hi.")
    if text is None:
        # label is the failure reason -> fall back honestly to heuristic
        text, label = heuristic_stuff(), "heuristic"

chat() NEVER raises and NEVER fakes output. On any failure (bad key,
rate limit, timeout, no network) it returns (None, reason).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

GROQ_BASE = "https://api.groq.com/openai/v1"
GROQ_MODEL_DEFAULT = "llama-3.3-70b-versatile"
GROQ_MODEL_FALLBACK = "llama-3.1-8b-instant"

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODEL_DEFAULT = "gemini-2.5-flash"

# Pollinations.ai — free, no signup, no API key. OpenAI-compatible endpoint.
POLLINATIONS_BASE = "https://text.pollinations.ai/openai"
POLLINATIONS_MODEL = "openai"  # their default free model alias

_TIMEOUT = 30


def _post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    # NOTE: a browser-like User-Agent is required — Cloudflare (error 1010)
    # blocks the default "Python-urllib/3.x" UA on some provider endpoints.
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) hackathon-demo/1.0",
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return {"ok": True, "status": resp.status,
                    "body": json.loads(resp.read().decode("utf-8", "replace"))}
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            detail = ""
        return {"ok": False, "status": e.code, "detail": detail}
    except Exception as e:  # DNS, timeout, TLS, offline...
        return {"ok": False, "status": 0, "detail": f"network error: {e}"}


def _groq_chat(key: str, model: str, system: str, user: str,
               max_tokens: int, temperature: float) -> dict:
    return _post_json(
        f"{GROQ_BASE}/chat/completions",
        {"model": model,
         "messages": [{"role": "system", "content": system},
                      {"role": "user", "content": user}],
         "temperature": temperature, "max_tokens": max_tokens},
        headers={"Authorization": f"Bearer {key}"},
    )


def _groq_extract(body: dict) -> str | None:
    try:
        return body["choices"][0]["message"]["content"].strip() or None
    except Exception:
        return None


def _gemini_chat(key: str, model: str, system: str, user: str,
                 max_tokens: int, temperature: float) -> dict:
    return _post_json(
        f"{GEMINI_BASE}/models/{model}:generateContent?key={key}",
        {"system_instruction": {"parts": [{"text": system}]},
         "contents": [{"role": "user", "parts": [{"text": user}]}],
         "generationConfig": {"temperature": temperature,
                              "maxOutputTokens": max_tokens}},
    )


def _gemini_extract(body: dict) -> str | None:
    try:
        parts = body["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or None
    except Exception:
        return None


def _pollinations_chat(system: str, user: str,
                       max_tokens: int, temperature: float) -> dict:
    """Pollinations.ai — no key needed. Tries POST (OpenAI-compatible) first,
    falls back to simple GET endpoint."""
    # Try OpenAI-compatible POST first
    r = _post_json(
        POLLINATIONS_BASE,
        {"model": POLLINATIONS_MODEL,
         "messages": [{"role": "system", "content": system},
                      {"role": "user", "content": user}],
         "temperature": temperature, "max_tokens": max_tokens},
    )
    if r["ok"]:
        return r
    # Fallback: simple GET endpoint (often faster/more reliable)
    try:
        import urllib.parse
        prompt = f"{system}\n\n{user}"
        url = f"https://text.pollinations.ai/{urllib.parse.quote(prompt[:2000])}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) hackathon-demo/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", "replace").strip()
            if text:
                return {"ok": True, "status": resp.status,
                        "body": {"choices": [{"message": {"content": text}}]}}
    except Exception as e:
        pass
    return r  # return original POST failure


def available_provider() -> str | None:
    """'pollinations' (always) | 'groq' | 'gemini' — based on env vars."""
    # Pollinations needs no key, so it's always available (network permitting)
    return "pollinations"


def chat(system: str, user: str, max_tokens: int = 512,
         temperature: float = 0.7) -> tuple[str | None, str]:
    """Try Pollinations (no key) -> Groq -> Gemini. Returns (text, 'pollinations' | 'groq:<model>' | 'gemini:<model>').

    On failure returns (None, reason) — e.g. (None, 'all providers failed').
    The caller MUST fall back honestly and label the fallback as heuristic.
    """
    # 0. Pollinations.ai — zero signup, zero key. Try first.
    #    Skip only if explicitly disabled via POLLINATIONS_DISABLE=1.
    if not os.environ.get("POLLINATIONS_DISABLE"):
        r = _pollinations_chat(system, user, max_tokens, temperature)
        if r["ok"]:
            text = _groq_extract(r["body"])  # same OpenAI-compatible format
            if text:
                return text, "pollinations:openai"
        # Don't give up — fall through to Groq/Gemini, then heuristic.
        pollinations_reason = f"HTTP {r['status']}" if r["status"] else r["detail"][:80]
    else:
        pollinations_reason = "disabled via POLLINATIONS_DISABLE=1"

    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    if groq_key:
        models = [os.environ.get("GROQ_MODEL", "").strip() or GROQ_MODEL_DEFAULT]
        if GROQ_MODEL_FALLBACK not in models:
            models.append(GROQ_MODEL_FALLBACK)
        last_reason = ""
        for model in models:
            r = _groq_chat(groq_key, model, system, user, max_tokens, temperature)
            if r["ok"]:
                text = _groq_extract(r["body"])
                if text:
                    return text, f"groq:{model}"
                last_reason = "empty response"
                continue
            status, detail = r["status"], r["detail"]
            if status == 401:
                return None, "groq: invalid API key (401) — check GROQ_API_KEY"
            if status == 429:
                return None, "groq: rate limited (429) — free tier quota hit, try later"
            if status in (400, 404) and "model" in detail.lower():
                last_reason = f"model '{model}' not available ({status})"
                continue  # try next model in the fallback chain
            last_reason = f"HTTP {status}: {detail[:120]}" if status else detail[:120]
        return None, f"groq failed: {last_reason}"

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if gemini_key:
        model = os.environ.get("GEMINI_MODEL", "").strip() or GEMINI_MODEL_DEFAULT
        r = _gemini_chat(gemini_key, model, system, user, max_tokens, temperature)
        if r["ok"]:
            text = _gemini_extract(r["body"])
            if text:
                return text, f"gemini:{model}"
            return None, "gemini: empty response"
        status, detail = r["status"], r["detail"]
        if status in (400, 403):
            return None, f"gemini: key invalid or blocked ({status}) — check GEMINI_API_KEY"
        if status == 429:
            return None, "gemini: rate limited (429) — free tier quota hit, try later"
        return None, f"gemini failed: HTTP {status}: {detail[:120]}" if status else f"gemini failed: {detail[:120]}"

    return None, f"no LLM available (pollinations: {pollinations_reason}; set GROQ_API_KEY or GEMINI_API_KEY)"


if __name__ == "__main__":
    # Quick self-test: python app/free_llm.py
    print("provider detected:", available_provider())
    text, label = chat("You are a test assistant.", "Reply with exactly: LLM_OK", max_tokens=20)
    print("label:", label)
    print("text:", (text or "")[:200])
