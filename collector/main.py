"""
Growisto - Claude Code telemetry collector.

This service is OPTIONAL and is not part of the default deployment. Every field
the hook captures fits inside GA4's limits, so GA4 direct mode is a complete
deployment on its own, and that is what the plugin does out of the box.

The one reason to run this: the GA4 api_secret stays here instead of being
copied onto every employee laptop, where anyone can read it and spam or poison
the property. That matters more the more laptops there are.

Events are written to the log rather than a warehouse. There was a BigQuery
sink here; it was removed along with the rest of the GCP path, because nobody
was running it and an untested write path is worse than no write path. If you
want SQL over the raw events later, add a log sink or reinstate a writer in
`persist()` -- that function is the only place that would need to change.

Full prompt text is never stored here. The hook sends at most a 100-character
scrubbed preview plus length, word count, and a hash, and there is no `prompt`
field to put full text in even if a future hook version tried.

Run it:

    pip install -r requirements.txt
    export INGEST_TOKEN=... GA4_MEASUREMENT_ID=G-XXXX GA4_API_SECRET=...
    uvicorn main:app --port 8080

Environment:
    INGEST_TOKEN         shared bearer token the hook must present (required)
    GA4_MEASUREMENT_ID   e.g. G-XXXXXXX      (optional; omit to skip GA4)
    GA4_API_SECRET       GA4 Measurement Protocol secret
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
log = logging.getLogger("cc-telemetry")

INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
GA4_MEASUREMENT_ID = os.environ.get("GA4_MEASUREMENT_ID", "")
GA4_API_SECRET = os.environ.get("GA4_API_SECRET", "")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))

GA4_ENDPOINT = "https://www.google-analytics.com/mp/collect"
GA4_PARAM_MAX = 100
MAX_EVENTS_PER_REQUEST = 200

app = FastAPI(title="Growisto Claude Code Telemetry Collector")


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
    """
    Emit each event as a structured log line.

    This is the only persistence the collector does. One line per event, JSON
    after the `cc_event` prefix, so a log sink can pick them up later without
    this service needing to know a warehouse exists.
    """
    for ev in events:
        row = ev.model_dump()
        row["event_time"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.gmtime(ev.ts_ms / 1000)
        )
        log.info("cc_event %s", json.dumps(row, default=str))
    return len(events)


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
        "warehouse": "logs-only",
        "retention_days": RETENTION_DAYS,
    }
