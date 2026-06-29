"""
Porter Capital Lead Intelligence — FastAPI application.
Stage 2A: read-only endpoints only.
Run locally: uvicorn app.api.main:app --reload --port 8000
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import dashboard, health, leads

app = FastAPI(
    title="Porter Capital Lead Intelligence API",
    version="0.1.0",
    description=(
        "Read-only API for Porter Capital Lead Intelligence. "
        "Research-ready leads only. Not sales-ready. No Salesforce push."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api")
app.include_router(dashboard.router, prefix="/api")
app.include_router(leads.router, prefix="/api")
