"""HTTP interface for the routing agent."""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .models import ComputeProfile, RouterPolicy
from .router import Router

app = FastAPI(title="Homelab Routing Agent")


class RouteRequest(BaseModel):
    """Incoming request body."""

    request: str


def load_router() -> Router:
    """Load local configuration, falling back to a useful development profile."""
    path = Path("config.json")
    data = json.loads(path.read_text()) if path.exists() else json.loads(Path("config.example.json").read_text())
    return Router([ComputeProfile(**profile) for profile in data["compute_profiles"]], RouterPolicy(**data["policy"]))


@app.post("/route")
def route(payload: RouteRequest) -> dict:
    """Classify and route one natural-language request."""
    try:
        return load_router().route(payload.request).to_dict()
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
