#!/usr/bin/env python3
"""Percentages and reset times from each agent's official usage page.

A dedicated Chrome profile holds the logins (``claudex usage login``). Reading
loads each usage page in that profile and takes the numbers from the JSON the
page itself requests, so nothing depends on page layout:

    codex   rate_limit.primary_window / secondary_window: used_percent, limit_window_seconds, reset_at
    companion  five_hour / seven_day / seven_day_<model>: utilization, resets_at
    grok    creditUsagePercent, currentPeriod {type, start, end}, productUsage
    qwen    per5HourPercentage / per1WeekPercentage with per*ResetTime

claudex never reads the profile's cookies, and it doesn't store response bodies
except as a redacted key outline with ``--debug``.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit

import browser
from providers import CODEX_ID, COMPANION_ID, GROK_ID, LABELS, LIMIT_PROVIDERS, QWEN_ID, USAGE_PAGES

WINDOW_BY_SECONDS = {5 * 3600: "five_hour", 7 * 86400: "weekly"}
_CHALLENGE = re.compile(r"just a moment|performing security verification|verify you are human|checking your browser", re.I)
_LOGIN_PATH = re.compile(r"log-?in|sign-?in|/auth\b|/oauth|/sso\b", re.I)
_SIGNED_OUT_TEXT = re.compile(r"not (?:currently )?(?:logged|signed) in|you need to log in|log in to access"
                              r"|sign in to continue", re.I)
_SIGNED_OUT_CODE = re.compile(r"need_?login|not_?login|unauthori[sz]ed", re.I)
_QWEN_KEY = re.compile(r"^per(\d+)(Hour|Day|Week|Month)Percentage$")


def page_domain(url: str) -> str:
    return ".".join((urlsplit(url).hostname or "").split(".")[-2:])


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _epoch(value):
    """ISO text, epoch seconds or epoch milliseconds as epoch seconds."""
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if re.fullmatch(r"\d+(\.\d+)?", text):
            value = float(text)
        else:
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    number = _number(value)
    if number is None or number <= 0:
        return None
    return number / 1000 if number > 1e11 else number


def window(name, used, resets=None, *, seconds=None, label=None) -> dict:
    used = None if used is None else round(max(0.0, used), 1)
    return {"name": name, "label": label or name, "used_percent": used,
            "remaining_percent": None if used is None else round(max(0.0, 100 - used), 1),
            "resets_at": resets, "window_seconds": seconds}


def _named(seconds, fallback):
    if seconds in WINDOW_BY_SECONDS:
        return WINDOW_BY_SECONDS[seconds]
    if isinstance(seconds, (int, float)) and seconds > 0:
        return f"{round(seconds / 86400)}_day" if seconds >= 86400 else f"{round(seconds / 3600)}_hour"
    return fallback


# --- extractors: one JSON object in, windows out --------------------------

def codex_windows(body: dict, now: float):
    limits = body.get("rate_limit")
    if not isinstance(limits, dict):
        return None
    found = []

    def add(rate, prefix=""):
        for slot, fallback in (("primary_window", "primary"), ("secondary_window", "secondary")):
            item = rate.get(slot) if isinstance(rate, dict) else None
            if not isinstance(item, dict) or _number(item.get("used_percent")) is None:
                continue
            seconds = _number(item.get("limit_window_seconds"))
            resets = _epoch(item.get("reset_at"))
            if resets is None and _number(item.get("reset_after_seconds")) is not None:
                resets = now + item["reset_after_seconds"]
            name = _named(seconds, fallback)
            found.append(window(f"{prefix}{name}", item["used_percent"], resets, seconds=seconds,
                                label=f"{prefix.replace('_', ' ')}{name}"))

    add(limits)
    for extra in body.get("additional_rate_limits") or []:
        if isinstance(extra, dict) and extra.get("limit_name"):
            add(extra.get("rate_limit"), f"{str(extra['limit_name']).lower()}_")
    return {"windows": found, "plan": body.get("plan_type")} if found else None


def companion_windows(body: dict, now: float):
    found = []
    for key, item in body.items():
        if not (key == "five_hour" or key.startswith("seven_day")) or not isinstance(item, dict):
            continue
        used = _number(item.get("utilization"))
        if used is None:
            continue
        name = "five_hour" if key == "five_hour" else "weekly" + key.removeprefix("seven_day")
        found.append(window(name, used, _epoch(item.get("resets_at")),
                            seconds=5 * 3600 if key == "five_hour" else 7 * 86400))
    return {"windows": found} if found else None


def grok_windows(body: dict, now: float):
    period = body.get("currentPeriod")
    used = _number(body.get("creditUsagePercent"))
    if not isinstance(period, dict) or used is None:
        return None
    kind = str(period.get("type") or "").upper()
    start, end = _epoch(period.get("start")), _epoch(period.get("end"))
    name = "weekly" if "WEEK" in kind else "monthly" if "MONTH" in kind else "period"
    seconds = end - start if start and end and end > start else None
    found = [window(name, used, end, seconds=seconds)]
    for product in body.get("productUsage") or []:
        if isinstance(product, dict) and product.get("product") and _number(product.get("usagePercent")) is not None:
            found.append(window(f"{name}_{str(product['product']).lower()}", product["usagePercent"], end,
                                seconds=seconds, label=f"{name} ({product['product']})"))
    return {"windows": found}


def qwen_windows(body: dict, now: float):
    found = []
    for key, value in body.items():
        match = _QWEN_KEY.match(key)
        used = _number(value)
        if not match or used is None:
            continue
        count, unit = int(match.group(1)), match.group(2)
        seconds = count * {"Hour": 3600, "Day": 86400, "Week": 7 * 86400, "Month": 30 * 86400}[unit]
        name = "monthly" if unit == "Month" else _named(seconds, f"{count}_{unit.lower()}")
        percent = used * 100 if used <= 1 else used  # the console reports a fraction or a percent
        found.append(window(name, percent, _epoch(body.get(f"per{count}{unit}ResetTime")), seconds=seconds))
    return {"windows": found} if found else None


EXTRACTORS = {CODEX_ID: codex_windows, COMPANION_ID: companion_windows, GROK_ID: grok_windows, QWEN_ID: qwen_windows}


def _objects(value, depth: int = 8):
    """Every JSON object in ``value``, parsing JSON text found in string fields (Alibaba's ``Data``)."""
    if depth < 0:
        return
    if isinstance(value, str) and value[:1] in "{[":
        try:
            value = json.loads(value)
        except ValueError:
            return
    if isinstance(value, dict):
        yield value
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return
    for child in children:
        yield from _objects(child, depth - 1)


def _signed_out(page: dict) -> bool:
    """Signed-out answers seen on these sites: ``{"status": "unauthenticated"}``, ``{"code": "ConsoleNeedLogin"}``."""
    for response in page.get("responses") or []:
        for candidate in _objects(response.get("body"), depth=2):
            if (str(candidate.get("status") or "").lower() == "unauthenticated"
                    or _SIGNED_OUT_CODE.search(str(candidate.get("code") or ""))):
                return True
    return bool(_SIGNED_OUT_TEXT.search(page.get("text") or ""))


def extract(provider: str, page: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    for response in reversed(page.get("responses") or []):
        for candidate in _objects(response.get("body")):
            found = EXTRACTORS[provider](candidate, now)
            if found and found["windows"]:
                parts = urlsplit(response.get("url") or "")
                return {"status": "ok", **found, "evidence": f"{parts.netloc}{parts.path}"}
    final = urlsplit(page.get("final_url") or "")
    requested = urlsplit(page.get("url") or "")
    if _CHALLENGE.search(f"{page.get('title') or ''}\n{(page.get('text') or '')[:400]}"):
        return {"status": "challenge", "reason": "the site showed a bot check; retry with --display screen and complete it"}
    if (_LOGIN_PATH.search(final.path) or _LOGIN_PATH.search(final.netloc) or _signed_out(page)
            or (final.netloc == requested.netloc and final.path in ("", "/") and requested.path not in ("", "/"))):
        return {"status": "login_required", "reason": f"not signed in; run: claudex usage login {provider}"}
    return {"status": "unparsed", "reason": "the page loaded but no usage data was recognized; rerun with --debug"}


# --- reading ----------------------------------------------------------------

def read_usage(providers, profile: Path, *, display: str = "virtual", binary=None, timeout: float = 45.0,
               debug_dir: Path | None = None) -> dict:
    report = {"read_at": time.time(), "display": display, "providers": {}}
    screen = browser.VirtualDisplay() if display == "virtual" else nullcontext()
    with screen as virtual:
        env = dict(os.environ, DISPLAY=virtual.display) if display == "virtual" else None
        chrome = browser.ChromePipe(profile, display="screen" if display == "virtual" else display,
                                    binary=binary, env=env)
        try:
            for provider in providers:
                url = USAGE_PAGES[provider]
                entry = {"label": LABELS[provider], "page": url}
                try:
                    page = chrome.capture(url, domains=(page_domain(url),), timeout=timeout,
                                          done=lambda page, provider=provider: extract(provider, page)["status"] == "ok")
                    entry.update(extract(provider, page))
                    if debug_dir is not None:
                        entry["debug_file"] = str(write_private(debug_dir / f"usage-debug-{provider}.json",
                                                                debug_outline(page)))
                except browser.BrowserError as error:
                    entry.update(status="error", reason=str(error))
                entry["checked_at"] = time.time()
                report["providers"][provider] = entry
        finally:
            chrome.close()
    return report


def debug_outline(page: dict) -> dict:
    """Response URLs and key paths with numbers and timestamps; other strings are redacted."""
    def outline(value, path="", out=None, depth=0):
        out = {} if out is None else out
        if depth > 10 or len(out) > 400:
            return out
        if isinstance(value, dict):
            for key, child in value.items():
                outline(child, f"{path}.{key}" if path else str(key), out, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value[:3]):
                outline(child, f"{path}[{index}]", out, depth + 1)
        elif isinstance(value, str):
            out[path] = value if re.fullmatch(r"[\d:.TZ+ -]{8,35}", value) else f"<text {len(value)}>"
        else:
            out[path] = value
        return out

    final = urlsplit(page.get("final_url") or "")
    return {"final_url": f"{final.netloc}{final.path}", "title": page.get("title"),
            "responses": [{"url": "".join(urlsplit(r.get("url") or "")[1:3]), "status": r.get("status"),
                           "keys": outline(r.get("body"))} for r in page.get("responses") or []]}


def write_private(path: Path, value) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def merge_cache(path: Path, report: dict) -> dict:
    """Keep earlier readings for providers this run didn't read."""
    try:
        cached = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = {}
    providers = cached.get("providers") if isinstance(cached.get("providers"), dict) else {}
    merged = {**report, "providers": {**providers, **report["providers"]}}
    write_private(path, merged)
    return merged


# --- report -----------------------------------------------------------------

def _until(seconds) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, minutes = rest // 3600, rest % 3600 // 60
    return f"{days}d {hours}h" if days else f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _paint(text, code, color):
    return f"\x1b[{code}m{text}\x1b[0m" if color else text


def format_report(report: dict, *, now: float | None = None, color: bool = False, width: int = 12) -> str:
    now = time.time() if now is None else now
    lines = [f"{'AGENT':<8} {'WINDOW':<18} {'USED':<{width + 6}} RESETS"]
    worst = 0.0
    for provider in [p for p in LIMIT_PROVIDERS if p in report.get("providers", {})]:
        entry = report["providers"][provider]
        if entry.get("status") != "ok":
            lines.append(f"{provider:<8} {_paint(entry.get('status', 'error'), '33', color):<18} {entry.get('reason', '')}")
            continue
        for item in entry.get("windows") or []:
            used = item.get("used_percent")
            worst = max(worst, used or 0)
            filled = round(width * min(100.0, used or 0) / 100)
            code = "31" if (used or 0) >= 100 else "33" if (used or 0) >= 80 else "32"
            bar = _paint("█" * filled, code, color) + "░" * (width - filled)
            resets = item.get("resets_at")
            when = "unknown"
            if resets:
                stamp = time.strftime("%a %H:%M" if resets - now < 6 * 86400 else "%b %d %H:%M", time.localtime(resets))
                when = "now" if resets <= now else f"in {_until(resets - now)} ({stamp})"
            label = str(item.get("label") or item.get("name")).replace("_", " ")
            lines.append(f"{provider:<8} {label[:18]:<18} {bar} {used:>5.1f}% {when}")
        age = now - entry.get("checked_at", now)
        if age > 3600:
            lines.append(f"{'':<8} {'':<18} read {_until(age)} ago")
    return "\n".join(lines)


def exhausted(report: dict) -> bool:
    return any((w.get("used_percent") or 0) >= 100 for e in report.get("providers", {}).values()
               if e.get("status") == "ok" for w in e.get("windows") or [])
