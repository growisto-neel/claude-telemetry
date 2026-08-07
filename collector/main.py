"""
Qualified Health - Claude Code telemetry collector.

This service is OPTIONAL. Every field the hook captures fits inside GA4's
limits, so GA4 direct mode is a complete deployment on its own. Two reasons to
run this anyway:

 1. The GA4 api_secret stays here instead of being copied onto every employee
    laptop, where anyone could read it and spam or poison the property.

 2. BigQuery gives you SQL over the raw events. The GA4 UI is workable for
    dashboards but poor for the "which skills does which team actually use"
    style of question.

Full prompt text is never stored here. The hook sends at most a 100-character
scrubbed preview plus length, word count, and a hash, and there is no `prompt`
column to put full text in even if a future hook version tried.

Deploy to Cloud Run:

    gcloud run deploy qh-cc-telemetry \
      --source . --region us-central1 --no-allow-unauthenticated \
      --set-env-vars GA4_MEASUREMENT_ID=G-XXXX,BQ_DATASET=analytics,BQ_TABLE=claude_code_events \
      --set-secrets GA4_API_SECRET=ga4-api-secret:latest,INGEST_TOKEN=cc-telemetry-token:latest

Environment:
    INGEST_TOKEN         shared bearer token the hook must present (required)
    GA4_MEASUREMENT_ID   e.g. G-XXXXXXX      (optional; omit to skip GA4)
    GA4_API_SECRET       GA4 Measurement Protocol secret
    BQ_DATASET/BQ_TABLE  BigQuery sink        (optional; omit to log only)
    RETENTION_DAYS       advisory, surfaced on /healthz
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("qh-cc-telemetry")

INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
GA4_MEASUREMENT_ID = os.environ.get("GA4_MEASUREMENT_ID", "")
GA4_API_SECRET = os.environ.get("GA4_API_SECRET", "")
BQ_DATASET = os.environ.get("BQ_DATASET", "")
BQ_TABLE = os.environ.get("BQ_TABLE", "")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))

GA4_ENDPOINT = "https://www.google-analytics.com/mp/collect"
GA4_PARAM_MAX = 100
MAX_EVENTS_PER_REQUEST = 200

app = FastAPI(title="QH Claude Code Telemetry Collector")

_bq_client = None


def bq_client():
    global _bq_client
    if _bq_client is None and BQ_DATASET and BQ_TABLE:
        from google.cloud import bigquery  # imported lazily

        _bq_client = bigquery.Client()
    return _bq_client


class Event(BaseModel):
    schema_version: int = 1
    event_name: str
    hook_event_name: str | None = None
    ts_ms: int
    user_email: str | None = None
    user_email_sha256: str | None = None
    user_id_source: str | None = None
    team: str | None = None
    client_id: str | None = None
    folder_path: str | None = None
    folder_name: str | None = None
    repo: str | None = None
    session_id: str | None = None
    session_source: str | None = None
    session_end_reason: str | None = None
    model: str | None = None
    permission_mode: str | None = None
    agent_id: str | None = None
    skill: str | None = None
    tool_name: str | None = None
    # No `prompt` field by design. The hook never sends full prompt text and
    # this model would silently drop it if a future version did; prompt_preview
    # is capped at 100 characters and prompt_chars records the real length.
    prompt_preview: str | None = None
    prompt_chars: int | None = None
    prompt_words: int | None = None
    prompt_sha256: str | None = None
    os: str | None = None
    hook_version: str | None = None
    tz_offset_min: int | None = None


class Batch(BaseModel):
    events: list[Event] = Field(default_factory=list)


def _safe_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def to_ga4_params(ev: Event) -> dict[str, Any]:
    """
    The GA4 projection. Every value is clipped to 100 characters because GA4
    truncates there silently, and clipping here keeps the warehouse row and the
    GA4 row identical rather than mysteriously different.
    """

    def clip(val: Any) -> Any:
        return None if val is None else str(val)[:GA4_PARAM_MAX]

    params = {
        "engagement_time_msec": 1,
        # Not named `session_id` on purpose: that param drives GA4's own session
        # stitching and a Claude session UUID would distort GA4 session counts.
        "cc_session_id": clip(ev.session_id),
        "user_email": clip(ev.user_email),
        "team": clip(ev.team),
        "folder_name": clip(ev.folder_name),
        "folder_path": (ev.folder_path or "")[-GA4_PARAM_MAX:] or None,
        "repo": clip(ev.repo),
        "skill": clip(ev.skill),
        "tool_name": clip(ev.tool_name),
        "model": clip(ev.model),
        "session_source": clip(ev.session_source),
        "permission_mode": clip(ev.permission_mode),
        "prompt_preview": clip(ev.prompt_preview),
        "prompt_chars": ev.prompt_chars,
        "prompt_words": ev.prompt_words,
        "prompt_hash": clip((ev.prompt_sha256 or "")[:16]) or None,
        "os": clip(ev.os),
        "hook_version": clip(ev.hook_version),
    }
    return {k: v for k, v in params.items() if v not in (None, "")}


async def forward_to_ga4(events: list[Event]) -> int:
    if not (GA4_MEASUREMENT_ID and GA4_API_SECRET):
        return 0
    url = f"{GA4_ENDPOINT}?measurement_id={GA4_MEASUREMENT_ID}&api_secret={GA4_API_SECRET}"
    sent = 0
    now_us = int(time.time() * 1_000_000)
    async with httpx.AsyncClient(timeout=10) as client:
        for ev in events:
            ts_us = ev.ts_ms * 1000
            # GA4 rejects events backdated more than 72h.
            if now_us - ts_us > 72 * 3600 * 1_000_000:
                ts_us = now_us
            body = {
                "client_id": ev.client_id or "unknown",
                "user_id": ev.user_email,
                "timestamp_micros": ts_us,
                "non_personalized_ads": True,
                "events": [{"name": ev.event_name, "params": to_ga4_params(ev)}],
            }
            try:
                resp = await client.post(url, json=body)
                if resp.status_code // 100 == 2:
                    sent += 1
                else:
                    log.warning("ga4 rejected: %s %s", resp.status_code, resp.text[:200])
            except Exception as exc:
                log.warning("ga4 error: %s", exc)
    return sent


def persist(events: list[Event]) -> int:
    client = bq_client()
    rows = []
    for ev in events:
        row = ev.model_dump()
        row["event_time"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.gmtime(ev.ts_ms / 1000)
        )
        rows.append(row)
    if client is None:
        # No warehouse configured: emit structured logs so Cloud Logging keeps
        # them and you can wire a log sink later.
        for row in rows:
            log.info("cc_event %s", json.dumps(row, default=str))
        return len(rows)
    table = f"{client.project}.{BQ_DATASET}.{BQ_TABLE}"
    errors = client.insert_rows_json(table, rows)
    if errors:
        log.error("bigquery insert errors: %s", errors[:3])
        raise HTTPException(500, "warehouse write failed")
    return len(rows)


@app.post("/v1/events")
async def ingest(batch: Batch, request: Request, authorization: str | None = Header(None)):
    if not INGEST_TOKEN:
        raise HTTPException(500, "collector misconfigured: INGEST_TOKEN unset")
    if not _safe_compare(authorization or "", f"Bearer {INGEST_TOKEN}"):
        raise HTTPException(401, "unauthorized")
    if not batch.events:
        return {"accepted": 0, "ga4_sent": 0}
    if len(batch.events) > MAX_EVENTS_PER_REQUEST:
        raise HTTPException(413, "too many events in one batch")

    stored = persist(batch.events)
    ga4_sent = await forward_to_ga4(batch.events)
    return {"accepted": stored, "ga4_sent": ga4_sent}


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "ga4_configured": bool(GA4_MEASUREMENT_ID and GA4_API_SECRET),
        "warehouse": f"{BQ_DATASET}.{BQ_TABLE}" if BQ_DATASET and BQ_TABLE else "logs-only",
        "retention_days": RETENTION_DAYS,
    }
