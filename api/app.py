"""Minimal standalone app for local Jobhunter API development."""
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.jobhunter import router as jobhunter_router

app = FastAPI(title="Jobhunter Demo API", docs_url=None, redoc_url=None, openapi_url=None)
origins = [
    item.strip()
    for item in os.environ.get(
        "JOBHUNTER_ALLOWED_ORIGINS",
        "http://127.0.0.1:4173,http://localhost:4173",
    ).split(",")
    if item.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)
app.include_router(jobhunter_router)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"ok": True}
