"""Vercel serverless entrypoint: wraps the FastAPI app with Mangum."""
from mangum import Mangum
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import app  # noqa: E402

_mangum = Mangum(app, lifespan="off")


def handler(event, context):
    """Map Vercel rewrite target back to / so FastAPI routes match.
    Vercel Python runtime may use 'rawPath' (API Gateway v2) or 'path' (v1).
    /api/* paths are NOT rewritten (see vercel.json) and pass through untouched.
    """
    event = dict(event)  # don't mutate the original
    # Normalize: check both possible path keys
    path = event.get("rawPath") or event.get("path", "/")
    if path == "/api/index":
        path = "/"
    # Set both keys so Mangum finds it regardless of version
    event["path"] = path
    event["rawPath"] = path
    # Also fix requestContext.http.path for v2 format
    rc = event.get("requestContext")
    if isinstance(rc, dict):
        http = rc.get("http")
        if isinstance(http, dict) and http.get("path") == "/api/index":
            http = dict(http)
            http["path"] = "/"
            rc = dict(rc)
            rc["http"] = http
            event["requestContext"] = rc
    return _mangum(event, context)
