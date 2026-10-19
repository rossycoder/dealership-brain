"""Persistent record store for SaaS data (accounts, workspaces, sessions,
tenant graph snapshots).

One job: put/get/delete/list small JSON records keyed by (kind, key), on a
real external store when one is configured, so data survives serverless
cold starts and is shared across instances. When nothing is configured,
get_store() returns None and callers fall back to in-memory behaviour
(honest demo-grade mode, reported by /api/status).

Backends, in preference order:
1. Upstash Redis over its REST API (httpx) — configured by the Vercel
   Marketplace one-click integration, which sets
   UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN
   (Vercel KV naming KV_REST_API_URL / KV_REST_API_TOKEN also accepted).
   No password is ever typed by a human or an agent: Vercel provisions
   the credentials itself.
2. Redis via a connection URL (REDIS_URL / KV_URL / UPSTASH_REDIS_URL)
   using redis-py (already a dependency of the FalkorDB client).
3. FalkorDB itself — records live as JSON `data` properties in a dedicated
   "saas_meta" graph (the original design; used when FALKOR_HOST points
   at a real server, e.g. a FalkorDB Cloud instance).

Every operation is best-effort: failures are swallowed by callers' mirror
semantics so a store outage never breaks a request.
"""
import json
import os

_store = None
_store_tried = False

_PREFIX = "dbbrain"


def _key(kind, key):
    return f"{_PREFIX}:{kind}:{key}"


class _RestStore:
    """Upstash Redis via REST: POST a JSON command array, Bearer token."""
    name = "upstash-redis"

    def __init__(self, url, token):
        self._url = url.rstrip("/")
        self._token = token

    def _cmd(self, *args):
        import httpx
        # trust_env=False: never route through ambient proxy env vars —
        # a malformed proxy config must not break store calls.
        with httpx.Client(timeout=6.0, trust_env=False) as c:
            r = c.post(self._url, json=list(args),
                       headers={"Authorization": f"Bearer {self._token}"})
        r.raise_for_status()
        return r.json().get("result")

    def put(self, kind, key_name, key, record):
        self._cmd("SET", _key(kind, key), json.dumps(record))

    def get(self, kind, key_name, key):
        raw = self._cmd("GET", _key(kind, key))
        return json.loads(raw) if raw else None

    def delete(self, kind, key_name, key):
        self._cmd("DEL", _key(kind, key))

    def all(self, kind):
        out, cursor = [], "0"
        for _ in range(50):  # bounded scan
            cursor, keys = self._cmd("SCAN", cursor, "MATCH",
                                     _key(kind, "*"), "COUNT", 200)
            for k in keys:
                raw = self._cmd("GET", k)
                if raw:
                    out.append(json.loads(raw))
            if str(cursor) == "0":
                break
        return out


class _RedisUrlStore:
    """Any Redis reached by connection URL (redis-py, lazy connect)."""
    name = "redis"

    def __init__(self, url):
        import redis
        self._r = redis.Redis.from_url(url, decode_responses=True,
                                       socket_timeout=6.0)

    def put(self, kind, key_name, key, record):
        self._r.set(_key(kind, key), json.dumps(record))

    def get(self, kind, key_name, key):
        raw = self._r.get(_key(kind, key))
        return json.loads(raw) if raw else None

    def delete(self, kind, key_name, key):
        self._r.delete(_key(kind, key))

    def all(self, kind):
        out = []
        for k in self._r.scan_iter(match=_key(kind, "*"), count=200):
            raw = self._r.get(k)
            if raw:
                out.append(json.loads(raw))
        return out


class _FalkorStore:
    """Records as JSON `data` properties in the 'saas_meta' FalkorDB graph."""
    name = "falkordb"

    def __init__(self, graph):
        self._g = graph

    def put(self, kind, key_name, key, record):
        self._g.query(
            f"MERGE (n:{kind} {{{key_name}: $k}}) SET n.data = $d",
            {"k": key, "d": json.dumps(record)})

    def get(self, kind, key_name, key):
        res = self._g.query(
            f"MATCH (n:{kind} {{{key_name}: $k}}) RETURN n.data", {"k": key})
        if res.result_set:
            return json.loads(res.result_set[0][0])
        return None

    def delete(self, kind, key_name, key):
        self._g.query(
            f"MATCH (n:{kind} {{{key_name}: $k}}) DETACH DELETE n",
            {"k": key})

    def all(self, kind):
        res = self._g.query(f"MATCH (n:{kind}) RETURN n.data")
        return [json.loads(r[0]) for r in res.result_set]


def get_store():
    """Return the configured store backend, or None (memory mode)."""
    global _store, _store_tried
    if _store is not None:
        return _store
    if _store_tried:
        return None
    _store_tried = True
    env = os.environ.get
    rest_url = env("UPSTASH_REDIS_REST_URL") or env("KV_REST_API_URL")
    rest_tok = env("UPSTASH_REDIS_REST_TOKEN") or env("KV_REST_API_TOKEN")
    if rest_url and rest_tok:
        _store = _RestStore(rest_url, rest_tok)
        return _store
    url = env("REDIS_URL") or env("KV_URL") or env("UPSTASH_REDIS_URL")
    if url:
        try:
            _store = _RedisUrlStore(url)
            return _store
        except Exception:
            _store = None
    try:
        from app import graph as G
        db, mode = G.get_db()
        if mode != "memory":
            _store = _FalkorStore(db.select_graph("saas_meta"))
    except Exception:
        _store = None
    return _store


def store_name():
    s = get_store()
    return s.name if s else "memory"
