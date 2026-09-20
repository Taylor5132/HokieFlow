"""Account UI state in the backend's existing RLS-protected saved_plans table.

One reserved, deterministic row per user stores the schedule and UI state in
payload. Ordinary saved-plan rows are untouched. No additional SQL is needed.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
from uuid import NAMESPACE_URL, uuid5

EMPTY_DATA = {"savedClass": None, "reduceMotion": False, "plans": [], "events": []}
KIND = "hokieflow_account_v1"
log = logging.getLogger(__name__)


class StorageError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def row_id(uid):
    return str(uuid5(NAMESPACE_URL, f"hokieflow:account:{uid}"))


def _failure(exc):
    # Never log provider messages: they may contain submitted data or tokens.
    code = str(getattr(exc, "code", ""))
    log.warning("Account storage failed (%s, code=%s)", type(exc).__name__,
                code if code.isalnum() else "unknown")
    if code == "23505":
        return StorageError("Your schedule changed in another tab. Reload and try again.", 409)
    if code in ("42501", "PGRST301", "PGRST302", "PGRST303"):
        return StorageError("Your account could not access saved data. Please sign in again.", 401)
    return StorageError("We couldn't access your saved schedule. Please try again shortly.")


@contextmanager
def _client(token):
    if not token:
        raise StorageError("Please sign in again to access your saved schedule.", 401)
    client = None
    try:
        from database import get_supabase_client
        client = get_supabase_client()
        # A fresh client per request prevents one user's JWT leaking into
        # another request. The database's auth.uid() policies remain enforced.
        client.postgrest.auth(token)
        yield client
    except StorageError:
        raise
    except Exception as exc:
        raise _failure(exc) from exc
    finally:
        if client is not None and client.options.httpx_client is not None:
            client.options.httpx_client.close()


def _read(client, uid):
    rows = (client.table("saved_plans").select("payload")
            .eq("user_id", uid).eq("id", row_id(uid)).limit(1).execute().data) or []
    if not rows:
        return None
    payload = rows[0].get("payload")
    if (not isinstance(payload, dict) or payload.get("kind") != KIND
            or type(payload.get("version")) is not int or payload["version"] < 1
            or not isinstance(payload.get("data"), dict)):
        raise StorageError("Your saved schedule couldn't be loaded. Please contact the HokieFlow team.")
    return payload


def load(uid, token):
    with _client(token) as client:
        payload = _read(client, uid)
    return {"data": {**deepcopy(EMPTY_DATA), **payload["data"]} if payload else deepcopy(EMPTY_DATA),
            "version": payload["version"] if payload else 0}


def save(uid, token, data, version):
    if not isinstance(data, dict) or type(version) is not int or version < 0:
        raise StorageError("Invalid account data. Reload and try again.", 400)
    clean = {k: deepcopy(data.get(k, default)) for k, default in EMPTY_DATA.items()}
    if (not isinstance(clean["events"], list) or len(clean["events"]) > 200
            or not isinstance(clean["plans"], list) or len(clean["plans"]) > 20
            or type(clean["reduceMotion"]) is not bool
            or clean["savedClass"] is not None and not isinstance(clean["savedClass"], dict)):
        raise StorageError("Invalid schedule or too many saved items.", 400)
    try:
        size = len(json.dumps(clean, allow_nan=False).encode())
    except (TypeError, ValueError):
        raise StorageError("Invalid account data.", 400)
    if size > 500_000:
        raise StorageError("Your saved data is too large. Remove an older plan and try again.", 400)
    with _client(token) as client:
        old = _read(client, uid)
        current = old["version"] if old else 0
        if version != current:
            raise StorageError("Your schedule changed in another tab. Reload and try again.", 409)
        payload = {"kind": KIND, "version": current + 1, "data": clean}
        if old is None:
            # A competing first save fails the primary key constraint instead
            # of silently overwriting the other tab's data with an upsert.
            rows = client.table("saved_plans").insert({
                "id": row_id(uid), "user_id": uid, "title": "HokieFlow account",
                "payload": payload,
            }).execute().data
        else:
            # Compare-and-swap in the UPDATE itself, not just the preceding read.
            rows = (client.table("saved_plans").update({
                "payload": payload, "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("user_id", uid).eq("id", row_id(uid))
                .eq("payload->>version", str(current)).execute().data)
        if not rows:
            raise StorageError("Your schedule changed in another tab. Reload and try again.", 409)
    return {"ok": True, "data": clean, "version": current + 1}
