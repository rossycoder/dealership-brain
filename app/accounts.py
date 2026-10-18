"""SaaS accounts + workspaces for Dealership Brain.

One workspace = one dealership (tenant). Each account owns exactly one
workspace in this MVP. Passwords are PBKDF2-hashed (stdlib only — no new
dependencies). Sessions are opaque bearer tokens kept server-side.

Storage: in-memory dicts, mirrored best-effort to a JSON file so accounts
survive restarts where the filesystem is writable (local / embedded mode).
On serverless (Vercel) the mirror lives in /tmp and is per-instance, so
accounts there are demo-grade until a persistent FalkorDB (FALKOR_HOST)
is configured. /api/status reports the persistence mode honestly.
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


def _save():
    try:
        with open(_store_path(), "w") as f:
            json.dump({"accounts": _accounts, "workspaces": _workspaces,
                       "sessions": _sessions}, f)
    except Exception:
        pass


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
    return {"account": _public_account(_accounts[email]),
            "workspace": _public_workspace(_workspaces[slug]),
            "token": token}, None


def authenticate(email, password):
    email = (email or "").strip().lower()
    acct = _accounts.get(email)
    if not acct or not secrets.compare_digest(_hash(password, acct["salt"]),
                                              acct["pass_hash"]):
        return None
    with _lock:
        token = _new_session(email)
        _save()
    return {"account": _public_account(acct),
            "workspace": _public_workspace(_workspaces[acct["workspace_slug"]]),
            "token": token}


def account_for_token(token):
    sess = _sessions.get(token or "")
    if not sess or sess["expires_at"] < int(time.time()):
        return None
    acct = _accounts.get(sess["email"])
    if not acct:
        return None
    out = _public_account(acct)
    out["workspace"] = _public_workspace(_workspaces[acct["workspace_slug"]])
    return out


def logout(token):
    with _lock:
        _sessions.pop(token or "", None)
        _save()


def workspace_for_slug(slug):
    ws = _workspaces.get(slug or "")
    return _public_workspace(ws) if ws else None
