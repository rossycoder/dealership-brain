"""SaaS accounts + workspaces for Dealership Brain.

One workspace = one dealership (tenant). Each account owns exactly one
workspace in this MVP. Passwords are PBKDF2-hashed (stdlib only — no new
dependencies). Sessions are opaque bearer tokens kept server-side.

Storage: in-memory dicts, mirrored best-effort to a JSON file so accounts
survive restarts where the filesystem is writable (local / embedded mode),
AND mirrored write-through to an external persistent store when one is
configured (app.store: Upstash Redis via Vercel's one-click integration,
a Redis URL, or a FalkorDB "saas_meta" graph via FALKOR_HOST). With a
store, signups survive serverless cold starts and are shared across
instances; without one, accounts on Vercel are demo-grade.
/api/status reports the persistence mode honestly.
"""
import hashlib
import json
import os
import re
import secrets
import threading
import time

_lock = threading.Lock()
_accounts = {}    # email -> {name, email, salt, pass_hash, workspace_slug, created_at}
_workspaces = {}  # slug  -> {slug, name, owner_email, plan, created_at, trial_days}
_sessions = {}    # token -> {email, expires_at}

TRIAL_DAYS = 14
SESSION_DAYS = 30


def _store_path():
    p = os.environ.get("SAAS_STORE_FILE")
    if p:
        return p
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(here, "falkordb-data")
    try:
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "saas_store.json")
    except OSError:
        return "/tmp/saas_store.json"


def _load():
    global _accounts, _workspaces, _sessions
    try:
        with open(_store_path()) as f:
            data = json.load(f)
        _accounts = data.get("accounts", {})
        _workspaces = data.get("workspaces", {})
        _sessions = data.get("sessions", {})
    except Exception:
        pass
    # The persistent store mirror wins over the local file cache — it is
    # shared across instances and survives cold starts.
    for rec in _meta_all("Account"):
        # Shape guard: only a real account record may land here (a foreign
        # record that merely carries an email field must never poison login).
        if rec.get("email") and rec.get("pass_hash"):
            _accounts[rec["email"]] = rec
    for rec in _meta_all("Workspace"):
        if rec.get("slug"):
            _workspaces[rec["slug"]] = rec
    now = int(time.time())
    for rec in _meta_all("Session"):
        tok = rec.get("token")
        if tok and rec.get("expires_at", 0) > now:
            _sessions[tok] = {"email": rec["email"],
                              "expires_at": rec["expires_at"]}


def _save():
    try:
        with open(_store_path(), "w") as f:
            json.dump({"accounts": _accounts, "workspaces": _workspaces,
                       "sessions": _sessions}, f)
    except Exception:
        pass


# --- Persistent mirror (external store, when configured) --------------------
# Accounts/workspaces/sessions are ALSO written to the store from app.store
# (Upstash Redis / Redis URL / FalkorDB "saas_meta" graph), so signups
# survive serverless cold starts and are shared across instances. With no
# store configured the mirror is skipped and behaviour is the demo-grade
# in-memory store. Records are single JSON documents, so no escaping issues
# can occur on any backend.
from app import store as _store_mod


def _meta_put(label, key_name, key_val, record):
    s = _store_mod.get_store()
    if s is None:
        return
    try:
        s.put(label, key_name, key_val, record)
    except Exception:
        pass


def _meta_get(label, key_name, key_val):
    s = _store_mod.get_store()
    if s is None:
        return None
    try:
        return s.get(label, key_name, key_val)
    except Exception:
        return None


def _meta_del(label, key_name, key_val):
    s = _store_mod.get_store()
    if s is None:
        return
    try:
        s.delete(label, key_name, key_val)
    except Exception:
        pass


def _meta_all(label):
    s = _store_mod.get_store()
    if s is None:
        return []
    try:
        return s.all(label)
    except Exception:
        return []


def store_name():
    """'upstash-redis' | 'redis' | 'falkordb' | 'memory' (for /api/status)."""
    return _store_mod.store_name()


def persistence_mode():
    return ("persistent" if _store_mod.get_store() is not None
            else "in-memory (resets on cold start)")


def _hydrate_account(email):
    """Pull one account (+ its workspace) from the persistent mirror into the
    local dicts. Returns the account record or None."""
    rec = _meta_get("Account", "email", email)
    if not rec:
        return None
    _accounts[email] = rec
    slug = rec.get("workspace_slug")
    if slug and slug not in _workspaces:
        ws = _meta_get("Workspace", "slug", slug)
        if ws:
            _workspaces[slug] = ws
    return rec


_load()


def _hash(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", (password or "").encode(),
                               bytes.fromhex(salt_hex), 200_000).hex()


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "dealership"


def _unique_slug(base):
    slug = base
    n = 2
    while slug in _workspaces or slug == "demo":
        slug = f"{base}-{n}"
        n += 1
    return slug


def _new_session(email):
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"email": email,
                        "expires_at": int(time.time()) + SESSION_DAYS * 86400}
    return token


def _public_account(a):
    return {"name": a["name"], "email": a["email"],
            "workspace_slug": a["workspace_slug"]}


def _public_workspace(w):
    out = dict(w)
    if out.get("plan") == "trial":
        elapsed = (int(time.time()) - out["created_at"]) // 86400
        out["trial_days_left"] = max(0, out.get("trial_days", TRIAL_DAYS) - elapsed)
    else:
        out["trial_days_left"] = None
    return out


def create_account(owner_name, email, password, dealership_name):
    """Create account + workspace. Returns (result_dict, None) or (None, error)."""
    email = (email or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return None, "valid email required"
    if len(password or "") < 8:
        return None, "password must be at least 8 characters"
    if not (dealership_name or "").strip():
        return None, "dealership name required"
    with _lock:
        if email in _accounts:
            return None, "an account with this email already exists"
        slug = _unique_slug(slugify(dealership_name))
        salt = secrets.token_hex(16)
        _workspaces[slug] = {
            "slug": slug, "name": dealership_name.strip(),
            "owner_email": email, "plan": "trial",
            "created_at": int(time.time()), "trial_days": TRIAL_DAYS,
        }
        _accounts[email] = {
            "name": (owner_name or "").strip(), "email": email,
            "salt": salt, "pass_hash": _hash(password, salt),
            "workspace_slug": slug, "created_at": int(time.time()),
        }
        token = _new_session(email)
        _save()
        _meta_put("Workspace", "slug", slug, _workspaces[slug])
        _meta_put("Account", "email", email, _accounts[email])
        _meta_put("Session", "token", token,
                  {"token": token, **_sessions[token]})
    return {"account": _public_account(_accounts[email]),
            "workspace": _public_workspace(_workspaces[slug]),
            "token": token}, None


def authenticate(email, password):
    email = (email or "").strip().lower()
    acct = _accounts.get(email)
    if acct is None:
        acct = _hydrate_account(email)
    if not acct or not secrets.compare_digest(_hash(password, acct["salt"]),
                                              acct["pass_hash"]):
        return None
    with _lock:
        token = _new_session(email)
        _save()
        _meta_put("Session", "token", token,
                  {"token": token, **_sessions[token]})
    return {"account": _public_account(acct),
            "workspace": _public_workspace(_workspaces[acct["workspace_slug"]]),
            "token": token}


def account_for_token(token):
    sess = _sessions.get(token or "")
    if sess is None:
        rec = _meta_get("Session", "token", token or "")
        if rec and rec.get("expires_at", 0) > int(time.time()):
            sess = {"email": rec["email"], "expires_at": rec["expires_at"]}
            _sessions[token] = sess
    if not sess or sess["expires_at"] < int(time.time()):
        return None
    acct = _accounts.get(sess["email"]) or _hydrate_account(sess["email"])
    if not acct:
        return None
    out = _public_account(acct)
    out["workspace"] = _public_workspace(_workspaces[acct["workspace_slug"]])
    return out


def logout(token):
    with _lock:
        _sessions.pop(token or "", None)
        _save()
        _meta_del("Session", "token", token or "")


def workspace_for_slug(slug):
    ws = _workspaces.get(slug or "")
    if ws is None:
        rec = _meta_get("Workspace", "slug", slug or "")
        if rec:
            _workspaces[slug] = rec
            ws = rec
    return _public_workspace(ws) if ws else None
