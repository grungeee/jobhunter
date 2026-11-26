"""Password-gated, fixture-only Jobhunter demo API.

This module deliberately makes no requests to job portals. Current robots.txt
and terms restrictions make automated collection from the included portals
unavailable in this public demo. The parser runs against synthetic HTML fixtures
so the multi-portal workflow can be demonstrated without harvesting listings.
"""
from __future__ import annotations

import hashlib
import hmac
import html
from html.parser import HTMLParser
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/v1/jobhunter", tags=["jobhunter"])
SESSION_COOKIE = "jobhunter_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
VISIT_ALERT_COOLDOWN_SECONDS = 10 * 60
_VISIT_LOCK = threading.Lock()
_LAST_VISIT_ALERT = 0.0

PROFILES: dict[str, dict[str, Any]] = {
    "operations-demo": {
        "id": "operations-demo",
        "label": "Industrial operations sample",
        "summary": "Fictional industrial operations criteria: coordination, planning, automation, production and quality.",
        "keywords": ["operations", "coordination", "planning", "maintenance", "quality", "automation", "production"],
        "categories": ["operations"],
    },
    "analytics-demo": {
        "id": "analytics-demo",
        "label": "Analytics sample",
        "summary": "Synthetic analysis, reporting, research and data criteria.",
        "keywords": ["analysis", "reporting", "research", "data", "dashboard"],
        "categories": ["analytics"],
    },
}

PORTALS: dict[str, dict[str, Any]] = {
    "ams": {
        "id": "ams",
        "label": "AMS alle jobs",
        "home_url": "https://jobs.ams.at/public/emps/",
        "live_available": False,
        "live_reason": "The current robots.txt disallows automated access below /public/emps/.",
    },
    "karriere-at": {
        "id": "karriere-at",
        "label": "karriere.at",
        "home_url": "https://www.karriere.at/jobs",
        "live_available": False,
        "live_reason": "Current Nutzungsbedingungen section 2.8.3 prohibits automated evaluation.",
    },
    "jobs-at": {
        "id": "jobs-at",
        "label": "jobs.at",
        "home_url": "https://www.jobs.at/",
        "live_available": False,
        "live_reason": "Live access is disabled until portal terms and an authorized feed are verified.",
    },
}

# Fictional listings only. No page is fetched and no real job advertisement is
# reproduced. Age is relative to the scan time so the newest-three ordering is
# stable while demo dates remain current.
FIXTURE_JOBS: dict[str, list[dict[str, str]]] = {
    "ams": [
        {"category": "operations", "title": "Operations Coordinator", "company": "Example Works", "location": "Sample City", "age_days": "0", "description": "Coordinate daily operations, planning, automation projects and quality checks."},
        {"category": "analytics", "title": "Data Reporting Analyst", "company": "Sample Systems", "location": "Remote", "age_days": "1", "description": "Prepare data reports, dashboards and clear analysis."},
        {"category": "operations", "title": "Maintenance Planner", "company": "Demo Manufacturing", "location": "Exampletown", "age_days": "2", "description": "Plan maintenance, coordinate schedules and automate workflows."},
        {"category": "analytics", "title": "Research Assistant, Data", "company": "Fictional Labs", "location": "Remote", "age_days": "3", "description": "Support research with data collection and reporting."},
        {"category": "operations", "title": "Quality Process Associate", "company": "Sample Industries", "location": "Sample City", "age_days": "4", "description": "Improve production processes and coordinate quality activities."},
        {"category": "analytics", "title": "Dashboard Specialist", "company": "Example Analytics", "location": "Exampletown", "age_days": "5", "description": "Build dashboards and explain data trends to teams."},
    ],
    "karriere-at": [
        {"category": "operations", "title": "Project Operations Assistant", "company": "Example Partners", "location": "Sample City", "age_days": "0", "description": "Coordinate project planning, schedules and quality follow-up."},
        {"category": "analytics", "title": "Business Data Analyst", "company": "Demo Consulting", "location": "Remote", "age_days": "1", "description": "Analyse data and prepare management reports."},
        {"category": "operations", "title": "Service Planning Coordinator", "company": "Fictional Services", "location": "Exampletown", "age_days": "2", "description": "Coordinate maintenance planning and service operations."},
        {"category": "analytics", "title": "Research and Reporting Associate", "company": "Sample Research", "location": "Remote", "age_days": "3", "description": "Conduct research and communicate data findings."},
        {"category": "operations", "title": "Quality Operations Assistant", "company": "Example Manufacturing", "location": "Sample City", "age_days": "4", "description": "Support quality processes and cross-team coordination."},
        {"category": "analytics", "title": "Reporting Dashboard Analyst", "company": "Demo Data Group", "location": "Exampletown", "age_days": "5", "description": "Maintain reporting dashboards and analyse performance."},
    ],
    "jobs-at": [
        {"category": "operations", "title": "Planning and Operations Associate", "company": "Sample Logistics", "location": "Exampletown", "age_days": "0", "description": "Coordinate operations, planning and service quality."},
        {"category": "analytics", "title": "Data Research Associate", "company": "Example Insights", "location": "Remote", "age_days": "1", "description": "Research data patterns and prepare clear reports."},
        {"category": "operations", "title": "Maintenance Operations Planner", "company": "Fictional Factory", "location": "Sample City", "age_days": "2", "description": "Plan maintenance schedules and coordinate field teams."},
        {"category": "analytics", "title": "Analytics Reporting Assistant", "company": "Demo Metrics", "location": "Remote", "age_days": "3", "description": "Support data analysis and dashboard reporting."},
        {"category": "operations", "title": "Process Quality Coordinator", "company": "Example Assembly", "location": "Exampletown", "age_days": "4", "description": "Coordinate quality checks and improve operating processes."},
        {"category": "analytics", "title": "Research Data Analyst", "company": "Sample Labs", "location": "Sample City", "age_days": "5", "description": "Analyse research data and share findings in dashboards."},
    ],
}


def _cookie_key() -> bytes:
    seed = os.environ.get("JOBHUNTER_SESSION_SECRET", "").strip()
    if len(seed) < 32:
        raise HTTPException(status_code=503, detail="Demo authentication is not configured.")
    return hmac.new(seed.encode(), b"jobhunter-session-v1", hashlib.sha256).digest()


def _cookie_secure() -> bool:
    return os.environ.get("JOBHUNTER_COOKIE_SECURE", "true").strip().casefold() not in {"0", "false", "no"}


def _new_session_token(now: int | None = None) -> str:
    issued = int(time.time() if now is None else now)
    nonce = secrets.token_urlsafe(18)
    body = f"{issued}.{nonce}"
    signature = hmac.new(_cookie_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def _valid_session_token(token: str, now: int | None = None) -> bool:
    parts = token.split(".")
    if len(parts) != 3 or not parts[0].isdigit() or not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", parts[1]):
        return False
    try:
        key = _cookie_key()
    except HTTPException:
        return False
    issued = int(parts[0])
    current = int(time.time() if now is None else now)
    if issued > current + 30 or current - issued > SESSION_TTL_SECONDS:
        return False
    body = f"{parts[0]}.{parts[1]}"
    expected = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts[2])


def require_session(request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token or not _valid_session_token(token):
        raise HTTPException(status_code=401, detail="Enter the access code to continue.")


@router.post("/login")
async def login(request: Request):
    expected = os.environ.get("JOBHUNTER_PASSCODE", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Demo authentication is not configured.")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request.")
    passcode = body.get("passcode") if isinstance(body, dict) else None
    if not isinstance(passcode, str) or len(passcode) > 256 or not hmac.compare_digest(passcode, expected):
        raise HTTPException(status_code=401, detail="Invalid access code.")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        _new_session_token(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/api/v1/jobhunter",
    )
    return response


@router.post("/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/api/v1/jobhunter", secure=_cookie_secure(), httponly=True, samesite="lax")
    return response


@router.get("/profiles", dependencies=[Depends(require_session)])
async def profiles():
    return {"profiles": [
        {"id": profile["id"], "label": profile["label"], "summary": profile["summary"],
         "keywords": list(profile["keywords"])}
        for profile in PROFILES.values()
    ]}


@router.get("/portals", dependencies=[Depends(require_session)])
async def portals():
    return {"portals": list(PORTALS.values()), "demo_only": True}


class _FixtureParser(HTMLParser):
    """Read synthetic article cards from generated local fixture HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs):
        values = dict(attrs)
        if tag == "article" and values.get("data-demo-job") == "1":
            self.current = {key.removeprefix("data-"): value or "" for key, value in values.items() if key.startswith("data-")}
            self.rows.append(self.current)

    def handle_endtag(self, tag: str):
        if tag == "article":
            self.current = None


def _fixture_html(portal_id: str, now: datetime) -> str:
    cards = []
    for index, item in enumerate(FIXTURE_JOBS[portal_id], 1):
        age = int(item["age_days"])
        posted = (now - timedelta(days=age)).isoformat(timespec="seconds").replace("+00:00", "Z")
        attrs = {
            "id": f"{portal_id}-sample-{index}",
            "category": item["category"],
            "title": item["title"],
            "company": item["company"],
            "location": item["location"],
            "posted": posted,
            "description": item["description"],
        }
        encoded = " ".join(f'data-{key}="{html.escape(value, quote=True)}"' for key, value in attrs.items())
        cards.append(f"<article data-demo-job=\"1\" {encoded}></article>")
    return "<!doctype html><main>" + "".join(cards) + "</main>"


def parse_fixture(html_text: str) -> list[dict[str, str]]:
    parser = _FixtureParser()
    parser.feed(html_text)
    return [dict(row) for row in parser.rows]


def _profile_score(job: dict[str, str], profile: dict[str, Any]) -> tuple[int, list[str]]:
    text = f"{job.get('title', '')} {job.get('description', '')}".casefold()
    matched = [word for word in profile["keywords"] if word.casefold() in text]
    return round(100 * len(matched) / max(1, len(profile["keywords"]))), matched


def run_demo_scan(portal_ids: list[str], profile_id: str, now: datetime | None = None) -> dict[str, Any]:
    if profile_id not in PROFILES:
        raise ValueError("Unknown sample profile.")
    if not isinstance(portal_ids, list) or not portal_ids or len(portal_ids) > len(PORTALS):
        raise ValueError("Select at least one portal.")
    if len(set(portal_ids)) != len(portal_ids) or any(portal_id not in PORTALS for portal_id in portal_ids):
        raise ValueError("Unknown or duplicate portal selection.")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    profile = PROFILES[profile_id]
    results = []
    for portal_id in portal_ids:
        candidates = parse_fixture(_fixture_html(portal_id, current))
        candidates = [job for job in candidates if job.get("category") in profile["categories"]]
        candidates.sort(key=lambda job: job.get("posted", ""), reverse=True)
        jobs = []
        for job in candidates[:3]:
            score, matched = _profile_score(job, profile)
            job.update({
                "portal": portal_id,
                "score": score,
                "matched_keywords": matched,
                "synthetic": True,
                "source_url": PORTALS[portal_id]["home_url"],
            })
            jobs.append(job)
        results.append({
            "portal": portal_id,
            "portal_label": PORTALS[portal_id]["label"],
            "mode": "synthetic-fixture",
            "live_available": False,
            "live_reason": PORTALS[portal_id]["live_reason"],
            "jobs": jobs,
        })
    return {
        "profile": {"id": profile["id"], "label": profile["label"]},
        "limit_per_portal": 3,
        "results": results,
        "notice": "Synthetic fixture results only. No job portal was contacted.",
    }


@router.post("/scan", dependencies=[Depends(require_session)])
async def scan(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid request.")
    portal_ids = body.get("portals")
    profile_id = body.get("profile_id")
    if not isinstance(profile_id, str) or not isinstance(portal_ids, list) or any(not isinstance(item, str) for item in portal_ids):
        raise HTTPException(status_code=422, detail="Choose a sample profile and one or more portals.")
    try:
        return run_demo_scan(portal_ids, profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


async def _send_visit_alert() -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_HOME_CHANNEL", "").strip()
    if not token or not chat:
        return False
    text = "Jobhunter demo opened. Visitor identity is not included in this alert."
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat, "text": text},
            )
        if response.status_code != 200:
            return False
        payload = response.json()
        return payload.get("ok") is True and isinstance(payload.get("result", {}).get("message_id"), int)
    except Exception:
        return False


@router.post("/visit")
async def visit():
    """Send one minimal Telegram notification per cooldown window."""
    global _LAST_VISIT_ALERT
    now = time.monotonic()
    with _VISIT_LOCK:
        if now - _LAST_VISIT_ALERT < VISIT_ALERT_COOLDOWN_SECONDS:
            return {"ok": True, "notification": "cooldown"}
        _LAST_VISIT_ALERT = now
    sent = await _send_visit_alert()
    return {"ok": True, "notification": "sent" if sent else "unavailable"}
