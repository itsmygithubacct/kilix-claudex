#!/usr/bin/env python3
"""Grok credit usage from the grok CLI login, not the usage website.

``grok login`` stores an access token in ``$GROK_HOME/auth.json``. The same
token authorizes ``https://cli-chat-proxy.grok.com/v1/billing?format=credits``,
which is the credit config the grok CLI logs as ``billing: fetched credits
config``. A missing or rejected login is ``login_required``; run ``grok login``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from providers import GROK_HOME_DIR, GROK_HOME_ENV
from usage_pages import grok_windows

BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
SOURCE = "grok cli billing"
_TIMEOUT = 20.0


class GrokStatusError(Exception):
    pass


def grok_home(value=None) -> Path:
    return Path(value or os.environ.get(GROK_HOME_ENV) or Path.home() / GROK_HOME_DIR)


def access_token(home: Path | None = None) -> str | None:
    """The newest grok CLI access token, or None when ``grok login`` has not been run."""
    try:
        data = json.loads((grok_home(home) / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    entries = [item for item in data.values() if isinstance(item, dict) and isinstance(item.get("key"), str) and item["key"]]
    if not entries:
        return None
    entries.sort(key=lambda item: str(item.get("expires_at") or ""), reverse=True)
    return entries[0]["key"]


def _client_version(home: Path) -> str | None:
    try:
        data = json.loads((home / "version.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version else None


def _fetch(token: str, version: str | None) -> tuple[int, dict | None]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if version:
        headers["x-grok-client-version"] = version
    request = urllib.request.Request(BILLING_URL, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            payload = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        payload = error.read()
        status = error.code
    try:
        body = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError):
        body = None
    return status, body if isinstance(body, dict) else None


def read(*, now: float | None = None, home=None, fetch=None) -> dict:
    """Read the Grok credit window. Failures become an entry so the other agents still report."""
    import time
    now = time.time() if now is None else now
    try:
        return _read(now=now, home=grok_home(home), fetch=fetch or _fetch)
    except GrokStatusError as error:
        return {"status": "error", "reason": str(error), "checked_at": now, "windows": [], "source": SOURCE}


def _read(*, now: float, home: Path, fetch) -> dict:
    token = access_token(home)
    if not token:
        return {"status": "login_required", "reason": "not signed in; run: grok login",
                "checked_at": now, "windows": [], "source": SOURCE}
    try:
        status, body = fetch(token, _client_version(home))
    except (OSError, urllib.error.URLError) as error:
        raise GrokStatusError(f"grok billing request failed ({error})") from error
    if status in (401, 403):
        return {"status": "login_required", "reason": "not signed in; run: grok login",
                "checked_at": now, "windows": [], "source": SOURCE}
    if status != 200 or not isinstance(body, dict):
        raise GrokStatusError(f"grok billing request failed ({status})")
    config = body.get("config")
    found = grok_windows(config, now) if isinstance(config, dict) else None
    if not found:
        raise GrokStatusError("grok billing response had no credit window")
    return {"status": "ok", "checked_at": now, "source": SOURCE, **found}
