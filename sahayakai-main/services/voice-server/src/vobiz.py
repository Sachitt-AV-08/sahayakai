"""
Vobiz media-stream support for the parent-call sidecar.

The web app (VOICE_PROVIDER=vobiz) dials via Vobiz and, on answer, hands Vobiz a
`<Stream>` verb pointing at THIS service's `/telephony/vobiz/stream` WebSocket
(see src/app/api/attendance/vobiz/answer/route.ts). Vobiz is a Plivo-compatible
platform, so its media socket speaks the Plivo AudioStream protocol and we
terminate it with Pipecat's PlivoFrameSerializer.

Everything Vobiz-specific lives here so bot.py / main.py stay provider-agnostic:
  - verify_stream_token(): validates the signed `t` query param (mirrors
    src/lib/vobiz/tokens.ts — HMAC-SHA256 over "vobiz-call.<outreachId>.<exp>").
  - fetch_context_firestore(): reads the parent_outreach record directly, so the
    sidecar needs no web-app internal key.
  - write_call_result_firestore(): writes transcript + callSummary back onto the
    same record, which is what the Contact-Parent modal renders.
  - persist_opt_out_firestore(): writes the do-not-call record the web app's
    dial routes check (call_suppressions/{sha256(E.164)}).
  - hangup_vobiz_call(): ends the leg over Vobiz REST (the answer XML sets
    keepCallAlive=true, so closing the socket alone does not drop the call).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

from loguru import logger

# Must match VOBIZ_DOMAINS.STREAM in src/lib/vobiz/tokens.ts.
_STREAM_DOMAIN = "vobiz-call"
_VOBIZ_API_BASE = "https://api.vobiz.ai/api/v1"


# ── Token verification (mirror of src/lib/vobiz/tokens.ts) ──────────────────

def verify_stream_token(token: str | None, signing_key: str | None) -> str | None:
    """Return the outreachId a stream token authorises, or None.

    Wire shape is `<outreachId>.<exp>.<sig>` where
    sig = base64url(HMAC_SHA256(key, "vobiz-call.<outreachId>.<exp>")) with no
    padding (Node's `digest('base64url')`).
    """
    if not token or not signing_key:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    outreach_id, exp_raw, sig = parts
    try:
        exp = int(exp_raw)
    except ValueError:
        return None
    if not outreach_id or not sig:
        return None
    if exp < int(time.time()):
        logger.warning("Vobiz stream token expired")
        return None

    message = f"{_STREAM_DOMAIN}.{outreach_id}.{exp}".encode("utf-8")
    digest = hmac.new(signing_key.strip().encode("utf-8"), message, hashlib.sha256).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    if not hmac.compare_digest(expected, sig):
        logger.warning("Vobiz stream token signature mismatch")
        return None
    return outreach_id


# ── Firestore context (self-contained; no web internal key needed) ──────────

def _db(project: str):
    # Imported lazily so unit tests / the Twilio path do not require the dep.
    from google.cloud import firestore  # type: ignore

    return firestore.AsyncClient(project=project)


async def fetch_context_firestore(outreach_id: str, project: str) -> dict[str, Any] | None:
    """Load the call context the bot needs from parent_outreach/{outreachId}.

    Shape mirrors GET /api/attendance/call-context so bot.py is unchanged.
    """
    try:
        snap = await _db(project).collection("parent_outreach").document(outreach_id).get()
        if not snap.exists:
            logger.error(f"parent_outreach/{outreach_id} not found")
            return None
        d = snap.to_dict() or {}
        return {
            "studentName": d.get("studentName", ""),
            "className": d.get("className", ""),
            "subject": d.get("subject", ""),
            "reason": d.get("reason", ""),
            "generatedMessage": d.get("generatedMessage", ""),
            "parentLanguage": d.get("parentLanguage", "Hindi"),
            "teacherName": d.get("teacherName", ""),
            "schoolName": d.get("schoolName", ""),
            "parentPhone": d.get("parentPhone", ""),
        }
    except Exception as e:  # noqa: BLE001 — a call must never crash on bookkeeping
        logger.error(f"Firestore context fetch failed for {outreach_id}: {e}")
        return None


async def write_call_result_firestore(
    outreach_id: str,
    project: str,
    transcript: list[dict],
    summary: dict | None,
) -> None:
    """Write transcript + callSummary onto the outreach record for the modal.

    callStatus itself is stamped by the web status webhook when Vobiz reports the
    hangup; we only add the conversation artefacts here.
    """
    try:
        update: dict[str, Any] = {
            "transcript": transcript,
            "turnCount": len(transcript),
            "updatedAt": _iso_now(),
        }
        if summary:
            update["callSummary"] = summary
        await _db(project).collection("parent_outreach").document(outreach_id).set(
            update, merge=True
        )
        logger.info(f"Wrote transcript+summary to parent_outreach/{outreach_id}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Firestore result write failed for {outreach_id}: {e}")


async def persist_opt_out_firestore(outreach_id: str, project: str) -> bool:
    """Save "do not call this parent again" where the web app's dial check reads it.

    Writes call_suppressions/{sha256(parentPhone)} (src/opt_out.py has the
    shape and why) and flags the outreach record. True when the suppression
    record was written. Logs the outcome; never logs the phone number.
    """
    from src.opt_out import persist_opt_out

    try:
        result = await persist_opt_out(_db(project), outreach_id)
    except Exception as e:  # noqa: BLE001 — e.g. the client itself could not be built
        logger.error(f"Opt-out write failed for {outreach_id}: {e}")
        return False
    if result.ok:
        logger.info(f"Opt-out saved for parent_outreach/{outreach_id}: {result.detail}")
    else:
        logger.error(f"Opt-out NOT saved for parent_outreach/{outreach_id}: {result.detail}")
    return result.ok


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ── Vobiz REST hangup (keepCallAlive=true means we must end the leg) ─────────

async def hangup_vobiz_call(call_uuid: str, auth_id: str, auth_token: str) -> None:
    """End the Vobiz leg via REST. No-op if we lack the uuid or creds."""
    if not (call_uuid and auth_id and auth_token):
        return
    import httpx  # lazy — keeps the module importable without the dep installed

    url = f"{_VOBIZ_API_BASE}/Account/{auth_id}/Call/{call_uuid}/"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.delete(url, auth=(auth_id, auth_token))
        if resp.status_code >= 300:
            logger.warning(f"Vobiz hangup returned {resp.status_code} for {call_uuid}")
        else:
            logger.info(f"Vobiz call {call_uuid} hung up")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Vobiz hangup error for {call_uuid}: {e}")
