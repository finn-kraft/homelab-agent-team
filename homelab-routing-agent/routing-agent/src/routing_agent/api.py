"""HTTP interface for the routing agent."""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .models import ComputeProfile, RouterPolicy
from .router import Router

app = FastAPI(title="Homelab Routing Agent")


class RouteRequest(BaseModel):
    """Incoming request body."""

    request: str = Field(min_length=1, max_length=12_000)


class InferenceRouteRequest(BaseModel):
    """Structured metadata supplied by an internal development agent.

    It is intentionally distinct from :class:`RouteRequest`: this endpoint
    chooses a model/backend, whereas ``/route`` chooses a domain agent and a
    generic compute target for an end-user request.
    """

    request: str = Field(min_length=1, max_length=12_000)
    caller_agent: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_.-]+$")
    task_type: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_.-]+$")
    attempt: int = Field(default=1, ge=1, le=100)
    complexity: str = Field(default="medium", pattern=r"^(light|medium|heavy)$")
    privacy_sensitive: bool = False
    needs_strong_model: bool = False
    local_failures: int = Field(default=0, ge=0, le=100)


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


@app.post("/route/inference")
def route_inference(payload: InferenceRouteRequest) -> dict:
    """Select an Ollama-first inference backend for an internal agent call."""
    try:
        return load_router().route_inference(**payload.model_dump()).to_dict()
    except (RuntimeError, ValueError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
