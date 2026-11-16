"""
Single use for Vobiz media-stream tokens.

The web app's answer webhook mints a signed token (vobiz-call.<outreachId>.<exp>)
and hands Vobiz a stream URL carrying it. verify_stream_token proves the token
is genuine and unexpired, but on its own it accepts the same token any number
of times until it expires. The URL is not secret in practice: it sits in the
answer XML Vobiz fetched, and in the Cloud Run request log. A replayed socket
would hear the call's greeting and write a transcript and summary onto that
parent's outreach record.

So the first connection CONSUMES the token: an atomic `create()` of
vobiz_stream_tokens/{sha256(token)}, which Firestore refuses if the document
already exists. Every later connection with the same token is refused before
any call context is loaded. The token itself is never stored, only its digest.

Pure on purpose (stdlib only; Firestore is passed in) so the tests can drive it.
"""

from __future__ import annotations

import enum
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

CONSUMED_COLLECTION = "vobiz_stream_tokens"

# Keep the consumed record a little past the token's own expiry, for a
# Firestore TTL policy on `expiresAt` to sweep.
_KEEP_AFTER_EXPIRY = timedelta(hours=1)


class StreamUse(enum.Enum):
    FIRST = "first"    # this connection consumed the token
    REPLAY = "replay"  # the token was already used; refuse
    ERROR = "error"    # could not record the use; refuse (fail closed)


def stream_token_id(token: str) -> str:
    """Document id for a token: its sha256 hex, never the token itself."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token_expiry(token: str) -> datetime | None:
    parts = token.split(".")
    try:
        return datetime.fromtimestamp(int(parts[1]), tz=timezone.utc)
    except (IndexError, ValueError, OverflowError, OSError):
        return None


def _already_exists(exc: BaseException) -> bool:
    """google.api_core.exceptions.AlreadyExists (a Conflict, HTTP 409), without importing it."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    return "AlreadyExists" in names or "Conflict" in names or getattr(exc, "code", None) == 409


async def consume_stream_token(db: Any, token: str, outreach_id: str, *, now: datetime | None = None) -> StreamUse:
    """Record the first use of a verified stream token. Never raises."""
    now = now or datetime.now(timezone.utc)
    expiry = _token_expiry(token) or now
    try:
        await db.collection(CONSUMED_COLLECTION).document(stream_token_id(token)).create(
            {
                "outreachId": outreach_id,
                "consumedAt": now,
                "expiresAt": expiry + _KEEP_AFTER_EXPIRY,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return StreamUse.REPLAY if _already_exists(exc) else StreamUse.ERROR
    return StreamUse.FIRST
