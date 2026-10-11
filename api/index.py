"""Vercel serverless entrypoint: wraps the FastAPI app with Mangum."""
from mangum import Mangum
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import app  # noqa: E402

_mangum = Mangum(app, lifespan="off")


def handler(event, context):
    """Map Vercel rewrite target /api/index back to / so FastAPI routes match.
    /api/* paths are NOT rewritten (see vercel.json) and pass through untouched.
    """
    path = event.get("path", "/")
    if path == "/api/index":
        event = dict(event)
        event["path"] = "/"
        event["rawPath"] = "/"
    return _mangum(event, context)
