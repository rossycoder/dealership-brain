"""Vercel serverless entrypoint: wraps the FastAPI app with Mangum."""
from mangum import Mangum
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import app  # noqa: E402

handler = Mangum(app, lifespan="off")
