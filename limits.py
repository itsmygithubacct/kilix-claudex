#!/usr/bin/env python3
"""Token-limit detection for coding session panes: weekly and five-hour windows.

Limits belong to the account, not to one pane. Evidence therefore comes from
the pane's own session file and the most recently written session files of the
same agent. Pane Center tracks Codex and companion panes; Grok and omp panes
are recognized from their foreground command.

Codex
    Rollout ``token_count`` events carry ``rate_limits`` snapshots. Each has a
    ``primary`` and an optional ``secondary`` window, with ``used_percent``,
    ``window_minutes`` (300 = five_hour, 10080 = weekly) and ``resets_at``.
    Some limit buckets report only a weekly window; the five-hour window is
    listed only where the bucket has one. The pane's newest ``limit_id`` is the
    current bucket; the newest snapshot of that bucket in any recent rollout
    wins.
Companion agent
    The companion agent writes no usage percentage locally. Its transcripts do
    record rejected API calls with ``quotaLimits`` (``rateLimitType``
    ``five_hour`` or ``seven_day``, ``resetsAt``). A companion window is
    therefore known exhausted until its reset time, or unknown.
Grok
    Grok Build logs ``billing: fetched credits config`` to
    ``$GROK_HOME/logs/unified.jsonl`` while a session is open. The record's
    ``creditUsagePercent`` and ``currentPeriod`` (weekly or monthly, with start
    and end) give the account allowance. Grok has no five-hour window.
Qwen
    Qwen Token Plan panes run omp with a token-plan model. omp sessions record no
    usage percentage, only failed requests (``stopReason`` ``error`` with an
    ``errorMessage``). A quota rejection counts until a later Token Plan request
    succeeds or its window has certainly elapsed.
Screen
    Warnings such as "less than 5% of your weekly limit left", "hit your usage
    limit" and "Allocated quota exceeded", matched only at the start of a line
    so quoted text elsewhere doesn't count. Scrollback can be stale, so fresh
    file evidence outranks it.

``state`` is ``exhausted``, ``low``, ``ok`` or ``unknown``. Unknown never blocks.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import time

from providers import (CODEX_HOME_DIR, CODEX_HOME_ENV, CODEX_ID, COMPANION_CONFIG_ENV,
                       COMPANION_HOME_DIR, COMPANION_ID, GROK_HOME_DIR, GROK_HOME_ENV, GROK_ID,
                       LIMIT_PROVIDERS, OMP_AGENT_DIR, OMP_AGENT_DIR_ENV, OMP_COMMAND, QWEN_ID,
                       QWEN_OMP_PROVIDERS)

WINDOW_NAMES = {300: "five_hour", 10080: "weekly"}
COMPANION_WINDOWS = {"five_hour": "five_hour", "seven_day": "weekly"}
FRESH_SECONDS = 3600
GROK_BILLING = b"billing: fetched credits config"
GROK_PERIODS = {"USAGE_PERIOD_TYPE_WEEKLY": "weekly", "USAGE_PERIOD_TYPE_MONTHLY": "monthly"}
# A rejection can't outlive its window. "quota" names a rejection that doesn't say which window.
QWEN_WINDOW_SECONDS = {"five_hour": 5 * 3600, "weekly": 7 * 86400, "quota": 7 * 86400}

_SESSION_ID = re.compile(r"[0-9A-Za-z-]{8,64}")
_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_DUST = re.compile("[⠀-⣿]")
_SCREEN_LOW = re.compile(
    r"^\W*(?:heads up,\s*)?you have less than (\d{1,3})% of your "
    r"(weekly|5h|5-hour|five-hour) limit left", re.I)
_SCREEN_REACHED = re.compile(
    r"^\W*(?:(?:you['’]ve|you have|you)\s+)?(?:hit|reached) (?:your|the) (?![^\n]*context)(?:[\w-]+ ){0,4}limit\b"
    r"|^\W*(?:\w+ )?usage limit reached"
    r"|^\W*(?:error\W*)?(?:429\W*)?(?:allocated quota exceeded|7-day limit exhausted)", re.I)
_SCREEN_NAMES = {"weekly": "weekly", "5h": "five_hour", "5-hour": "five_hour", "five-hour": "five_hour"}
_QUOTA_ERROR = re.compile(r"allocated quota exceeded|quota (?:exceeded|exhausted|reached)|limit exhausted"
                          r"|insufficient[_ ]quota|usage limit", re.I)
_DASHSCOPE_TOKEN_LIMIT = re.compile(r"error-code[^()\s]*#token-limit", re.I)  # a per-request token cap
_FIVE_HOUR_TEXT = re.compile(r"\b(?:5|five)[- ]?h(?:ou)?r?s?\b", re.I)
_WEEKLY_TEXT = re.compile(r"\b(?:7|seven)[- ]?days?\b|\bweek", re.I)


def codex_home(value=None) -> Path:
    return Path(value or os.environ.get(CODEX_HOME_ENV) or Path.home() / CODEX_HOME_DIR)


def companion_home(value=None) -> Path:
    return Path(value or os.environ.get(COMPANION_CONFIG_ENV) or Path.home() / COMPANION_HOME_DIR)


def grok_home(value=None) -> Path:
    return Path(value or os.environ.get(GROK_HOME_ENV) or Path.home() / GROK_HOME_DIR)


def omp_home(value=None) -> Path:
    return Path(value or os.environ.get(OMP_AGENT_DIR_ENV) or Path.home() / OMP_AGENT_DIR)


def _epoch(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _subdirs(path: Path) -> list[Path]:
    try:
        return sorted((p for p in path.iterdir() if p.is_dir()), reverse=True)
    except OSError:
        return []


def tail_lines(path, needle: bytes, *, chunk: int = 1 << 20, limit: int = 400):
    """Yield up to ``limit`` complete lines containing ``needle``, newest first."""
    try:
        stream = open(path, "rb")
    except OSError:
        return
    found = 0
    with stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        carry = b""
        while position > 0:
            size = min(chunk, position)
            position -= size
            stream.seek(position)
            lines = (stream.read(size) + carry).split(b"\n")
            carry = lines.pop(0) if position > 0 else b""  # may be a partial line
            for line in reversed(lines):
                if needle in line:
                    yield line
                    found += 1
                    if found >= limit:
                        return


def _window(*, now, used=None, resets=None, **extra) -> dict:
    resets = _number(resets)
    return {
        "used_percent": used,
        "remaining_percent": None if used is None else max(0.0, round(100.0 - used, 1)),
        "resets_at": resets,
        "resets_in_seconds": None if resets is None else max(0, round(resets - now)),
        "reset_since_observed": resets is not None and resets <= now,
        **extra,
    }


# --- codex ----------------------------------------------------------------

def recent_codex_rollouts(home=None, count: int = 6) -> list[Path]:
    """Newest writes, including sessions that started in an older directory."""
    candidates = (codex_home(home) / "sessions").glob("*/*/*/rollout-*.jsonl")
    return sorted(candidates, key=_mtime, reverse=True)[:count]


def codex_snapshots(path) -> list[tuple[str, float | None, dict]]:
    """Newest ``(limit_id, observed_at, rate_limits)`` per limit id, newest first."""
    seen: dict[str, tuple[str, float | None, dict]] = {}
    for line in tail_lines(path, b'"rate_limits"'):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        limits = payload.get("rate_limits") if isinstance(payload, dict) else None
        if not isinstance(limits, dict):
            continue
        limit_id = str(limits.get("limit_id") or "unknown")
        seen.setdefault(limit_id, (limit_id, _epoch(record.get("timestamp")), limits))
    return list(seen.values())


def _codex_windows(limits: dict, now: float) -> dict:
    windows = {}
    for slot in ("primary", "secondary"):
        window = limits.get(slot)
        if not isinstance(window, dict):
            continue
        used = _number(window.get("used_percent"))
        if used is None:
            continue
        minutes = window.get("window_minutes")
        name = WINDOW_NAMES.get(minutes, f"{minutes}_minutes")
        windows[name] = _window(now=now, used=float(used), resets=window.get("resets_at"),
                                window_minutes=minutes, source="codex rollout")
    return windows


def codex_evidence(session: dict, *, now: float, home=None) -> dict | None:
    own = session.get("path") if isinstance(session.get("path"), str) else None
    files = [Path(own)] if own and os.path.isfile(own) else []
    files += [path for path in recent_codex_rollouts(home) if str(path) != own]
    newest: dict[str, tuple[float | None, dict, str]] = {}
    current = None
    for index, path in enumerate(files):
        snapshots = codex_snapshots(path)
        if index == 0 and own and snapshots:
            current = snapshots[0][0]
        for limit_id, observed, limits in snapshots:
            if limit_id not in newest or (observed or 0) > (newest[limit_id][0] or 0):
                newest[limit_id] = (observed, limits, str(path))
    if not newest:
        return None
    if current is None:
        current = max(newest, key=lambda key: newest[key][0] or 0)
    note = None
    observed, limits, source_file = newest[current]
    windows = _codex_windows(limits, now)
    if not windows:
        with_windows = [key for key in newest if _codex_windows(newest[key][1], now)]
        if with_windows:
            fallback = max(with_windows, key=lambda key: newest[key][0] or 0)
            note = f"limit bucket {current} reports no windows; showing {fallback}"
            current = fallback
            observed, limits, source_file = newest[current]
            windows = _codex_windows(limits, now)
    return {
        "source": "codex rollout", "source_file": source_file, "limit_id": current,
        "plan_type": limits.get("plan_type"), "observed_at": observed,
        "age_seconds": None if observed is None else max(0, round(now - observed)),
        "reached": limits.get("rate_limit_reached_type"), "windows": windows, "note": note,
        "other_limit_ids": sorted(key for key in newest if key != current),
    }


# --- companion ------------------------------------------------------------

def recent_companion_transcripts(home=None, count: int = 12, session_id=None) -> list[Path]:
    root = companion_home(home) / "projects"
    own: list[Path] = []
    if isinstance(session_id, str) and _SESSION_ID.fullmatch(session_id):
        own = sorted(root.glob(f"*/{session_id}.jsonl"), key=_mtime, reverse=True)[:1]
    others = sorted(root.glob("*/*.jsonl"), key=_mtime, reverse=True)[:count]
    return own + [path for path in others if path not in own]


def _find(value, key, depth=6):
    if depth < 0:
        return None
    if isinstance(value, dict):
        if key in value:
            return value[key]
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = _find(child, key, depth - 1)
        if found is not None:
            return found
    return None


def companion_evidence(session: dict, *, now: float, home=None) -> dict | None:
    windows: dict[str, dict] = {}
    for path in recent_companion_transcripts(home, session_id=session.get("session_id")):
        for line in tail_lines(path, b'"quotaLimits"', limit=50):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            quota = _find(record, "quotaLimits")
            if not isinstance(quota, dict):
                continue
            kind = str(quota.get("rateLimitType") or "unknown")
            name = COMPANION_WINDOWS.get(kind) or (
                "weekly_" + kind.removeprefix("seven_day_") if kind.startswith("seven_day_") else kind)
            observed = _epoch(record.get("timestamp")) if isinstance(record, dict) else None
            if name in windows and (windows[name]["observed_at"] or 0) >= (observed or 0):
                continue
            # `utilization` appears on allowed_warning in the companion agent's bundle, but hasn't
            # been seen in local transcripts. Its scale is ambiguous, so <= 1 counts as a fraction.
            utilization = _number(quota.get("utilization"))
            used = None if utilization is None else round(utilization * 100 if utilization <= 1 else utilization, 1)
            windows[name] = _window(now=now, used=used, resets=quota.get("resetsAt"),
                                    status=quota.get("status"), limit_type=kind, observed_at=observed,
                                    overage_status=quota.get("overageStatus"),
                                    source="companion transcript", source_file=str(path))
    if not windows:
        return None
    observed = max((w["observed_at"] or 0) for w in windows.values()) or None
    return {"source": "companion transcript", "limit_id": COMPANION_ID, "observed_at": observed,
            "age_seconds": None if observed is None else max(0, round(now - observed)),
            "reached": None, "windows": windows,
            "note": "the companion agent records limits only when a request is rejected; usage percentages are not available"}


# --- grok -----------------------------------------------------------------

def grok_log(home=None) -> Path:
    return grok_home(home) / "logs" / "unified.jsonl"


def grok_snapshots(home=None, limit: int = 400):
    """Yield ``(observed_at, config, context)`` from billing log records, newest first."""
    for line in tail_lines(grok_log(home), GROK_BILLING, limit=limit):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        context = record.get("ctx") if isinstance(record, dict) else None
        config = context.get("config") if isinstance(context, dict) else None
        observed = _epoch(record.get("ts")) if isinstance(record, dict) else None
        if isinstance(config, dict) and observed is not None:
            yield observed, config, context


def _grok_period(config: dict) -> tuple[str, str, float | None, float | None]:
    period = config.get("currentPeriod")
    period = period if isinstance(period, dict) else {}
    kind = str(period.get("type") or "")
    name = GROK_PERIODS.get(kind) or kind.removeprefix("USAGE_PERIOD_TYPE_").lower() or "period"
    return name, kind, _epoch(period.get("start")), _epoch(period.get("end"))


def _amount(value):
    return _number(value.get("val")) if isinstance(value, dict) else _number(value)


def grok_evidence(*, now: float, home=None) -> dict | None:
    for observed, config, context in grok_snapshots(home, limit=20):
        used = _number(config.get("creditUsagePercent"))
        if used is None:
            continue
        name, kind, start, end = _grok_period(config)
        minutes = round((end - start) / 60) if start is not None and end is not None and end > start else None
        prepaid = _amount(config.get("prepaidBalance"))
        tier = context.get("subscriptionTier")
        note = "grok reports its allowance as a weekly or monthly credit period, with no five-hour window"
        if prepaid:
            note += f"; prepaid balance {prepaid:g} may keep requests working after the allowance is used"
        return {
            "source": "grok log", "source_file": str(grok_log(home)), "limit_id": GROK_ID,
            "plan_type": tier if isinstance(tier, str) else None, "observed_at": observed,
            "age_seconds": max(0, round(now - observed)), "reached": None,
            "windows": {name: _window(now=now, used=float(used), resets=end, window_minutes=minutes,
                                      period_type=kind, source="grok log")},
            "prepaid_balance": prepaid, "on_demand_used": _amount(config.get("onDemandUsed")),
            "on_demand_cap": _amount(config.get("onDemandCap")), "note": note,
        }
    return None


def grok_series(home=None, resets_at=None, window_minutes=None, limit: int = 5000) -> list[tuple[float, float]]:
    """``(observed_at, used_percent)`` pairs for the current credit period, oldest first."""
    start = resets_at - window_minutes * 60 if resets_at is not None and window_minutes else None
    latest = {}
    for observed, config, _ in grok_snapshots(home, limit=limit):
        if start is not None and observed < start:
            break
        used = _number(config.get("creditUsagePercent"))
        end = _grok_period(config)[3]
        if used is None or (resets_at is not None and end is not None and abs(end - resets_at) > RESET_TOLERANCE):
            continue
        latest.setdefault(observed, float(used))
    return sorted(latest.items())


# --- qwen (omp) -----------------------------------------------------------

def _omp_header(path: Path) -> dict:
    try:
        with open(path, "rb") as stream:
            for _, line in zip(range(5), stream):
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("type") == "session":
                    return record
    except OSError:
        pass
    return {}


def recent_omp_sessions(home=None, count: int = 12, cwd=None) -> list[Path]:
    """Recently written omp sessions, the newest one started in ``cwd`` first."""
    files = sorted((omp_home(home) / "sessions").glob("*/*.jsonl"), key=_mtime, reverse=True)
    own = next((path for path in files[:count * 4] if cwd and _omp_header(path).get("cwd") == cwd), None)
    return ([own] if own else []) + [path for path in files[:count] if path != own]


def omp_model(cwd, home=None) -> str | None:
    """The model most recently selected in the newest omp session started in ``cwd``."""
    sessions = recent_omp_sessions(home, cwd=cwd)
    if not cwd or not sessions or _omp_header(sessions[0]).get("cwd") != cwd:
        return None
    for line in tail_lines(sessions[0], b'"model_change"', limit=5):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("model"), str):
            return record["model"]
    return None


def qwen_quota_window(text: str) -> str | None:
    """The Token Plan window a failed request names, or None when it isn't a quota rejection."""
    if _DASHSCOPE_TOKEN_LIMIT.search(text) or not _QUOTA_ERROR.search(text):
        return None
    if _FIVE_HOUR_TEXT.search(text):
        return "five_hour"
    return "weekly" if _WEEKLY_TEXT.search(text) else "quota"


def qwen_evidence(session: dict, *, now: float, home=None, days: int = 8) -> dict | None:
    last_success, errors = None, []
    for path in recent_omp_sessions(home, cwd=session.get("cwd")):
        for line in tail_lines(path, b'"assistant"', limit=200):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            message = record.get("message") if isinstance(record, dict) else None
            if (not isinstance(message, dict) or message.get("role") != "assistant"
                    or message.get("provider") not in QWEN_OMP_PROVIDERS):
                continue
            observed = _epoch(record.get("timestamp"))
            if observed is None or observed < now - days * 86400:
                continue
            if message.get("stopReason") != "error":
                last_success = max(last_success or 0.0, observed)
                continue
            text = str(message.get("errorMessage") or "")
            name = qwen_quota_window(text)
            if name:
                errors.append((observed, name, text, str(path), message.get("model")))
    if not errors and last_success is None:
        return None
    errors.sort(key=lambda error: error[0], reverse=True)
    windows: dict[str, dict] = {}
    for observed, name, text, path, model in errors:
        if name in windows:
            continue
        window = _window(now=now, status="rejected", limit_type=name, observed_at=observed,
                         message=text[:200], model=model, source="omp session", source_file=path)
        superseded = last_success is not None and last_success > observed
        if superseded:
            window["superseded_by_success_at"] = last_success
        window["reset_since_observed"] = superseded or observed + QWEN_WINDOW_SECONDS[name] <= now
        windows[name] = window
    observed = max([error[0] for error in errors] + [last_success or 0.0]) or None
    return {"source": "omp session", "limit_id": QWEN_ID, "observed_at": observed,
            "age_seconds": None if observed is None else max(0, round(now - observed)),
            "reached": None, "windows": windows, "last_success_at": last_success,
            "events": [{"observed_at": o, "limit_type": n, "status": "rejected", "message": t[:200]}
                       for o, n, t, _, _ in errors[:20]],
            "note": "omp records no Token Plan usage percentage; rejected requests are the only local evidence"}


# --- pane detection -------------------------------------------------------

def _flag(argv: list[str], name: str) -> str | None:
    for index, value in enumerate(argv):
        if value == name and index + 1 < len(argv):
            return argv[index + 1]
        if value.startswith(name + "="):
            return value.split("=", 1)[1]
    return None


def pane_agent(pane, *, omp_home=None) -> tuple[str, dict]:
    """``(agent, session)`` from Pane Center, or from the foreground command of agents it doesn't track."""
    if not isinstance(pane, dict):
        return "", {}
    session = pane.get("coding_session")
    if isinstance(session, dict) and session.get("provider"):
        return str(session["provider"]), session
    process = pane.get("process") if isinstance(pane.get("process"), dict) else {}
    commands = [item for item in process.get("foreground") or [] if isinstance(item, dict)]
    commands.append({"argv": process.get("argv"), "cwd": pane.get("cwd")})
    for command in commands:
        argv = command.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(value, str) for value in argv):
            continue
        cwd = command.get("cwd") if isinstance(command.get("cwd"), str) else pane.get("cwd")
        name = os.path.basename(argv[0]).casefold()
        found = {"cwd": cwd, "detected_from": "process"}
        if name == GROK_ID:
            return GROK_ID, found
        if name == OMP_COMMAND:
            model = _flag(argv, "--model")
            if not model or "/" not in model:  # a fuzzy model name: the session knows the provider
                model = omp_model(cwd, omp_home) or model
            if model and model.split("/", 1)[0] in QWEN_OMP_PROVIDERS:
                return QWEN_ID, {**found, "model": model}
    return "", {}


# --- screen ---------------------------------------------------------------

def screen_evidence(text) -> dict | None:
    windows: dict[str, dict] = {}
    reached = False
    for raw in _DUST.sub("", _ESCAPE.sub("", text or "")).splitlines():
        line = raw.strip()
        low = _SCREEN_LOW.match(line)
        if low:
            windows[_SCREEN_NAMES[low.group(2).lower()]] = {"remaining_below_percent": int(low.group(1))}
        elif _SCREEN_REACHED.match(line):
            reached = True
    return {"windows": windows, "reached": reached} if windows or reached else None


# --- verdict --------------------------------------------------------------

# --- history --------------------------------------------------------------

RESET_TOLERANCE = 120  # rollouts report one reset time with a few seconds of jitter
LOOKBACK_SECONDS = {"five_hour": 3600, "weekly": 6 * 3600}


def rollouts_since(home=None, since: float = 0.0) -> list[Path]:
    """Every rollout written since ``since``, oldest first.

    Long sessions keep writing to their start day's directory, so this filters
    by modification time rather than by directory date.
    """
    root = codex_home(home) / "sessions"
    return sorted((path for path in root.glob("*/*/*/rollout-*.jsonl") if _mtime(path) >= since),
                  key=_mtime)


class CodexHistory:
    """Incremental reader of Codex ``rate_limits`` snapshots across rollouts.

    Offsets are remembered per file, so a long-running dashboard only reads
    what was appended since the last refresh.
    """

    def __init__(self, home=None, retain_seconds: float = 8 * 86400, chunk: int = 8 << 20):
        self.home = home
        self.retain_seconds = retain_seconds
        self.chunk = chunk
        self.offsets: dict[str, int] = {}
        self.points: dict[tuple[str, str], list[tuple[float, float, float | None]]] = {}

    def refresh(self, now: float | None = None) -> "CodexHistory":
        now = time.time() if now is None else now
        horizon = now - self.retain_seconds
        for path in rollouts_since(self.home, horizon):
            self._read(path)
        for key, series in self.points.items():
            self.points[key] = [point for point in series if point[0] >= horizon]
        return self

    def _read(self, path: Path) -> None:
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            return
        start = self.offsets.get(key, 0)
        if size < start:  # replaced or truncated
            start = 0
        if size == start:
            return
        position, carry = start, b""
        needle = b'"rate_limits"'
        try:
            with open(path, "rb") as stream:
                stream.seek(start)
                while position < size:
                    block = stream.read(min(self.chunk, size - position))
                    if not block:
                        break
                    position += len(block)
                    data = carry + block
                    complete = data.rfind(b"\n") + 1  # only whole lines; the rest waits
                    carry = data[complete:]
                    # Byte search instead of splitting every line: rollouts are mostly
                    # other records, and this keeps a multi-hundred-megabyte first read fast.
                    hit = data.find(needle, 0, complete)
                    while hit != -1:
                        line_start = data.rfind(b"\n", 0, hit) + 1
                        line_end = data.find(b"\n", hit, complete)
                        self._take(data[line_start:line_end])
                        hit = data.find(needle, line_end + 1, complete)
        except OSError:
            return
        self.offsets[key] = position - len(carry)

    def _take(self, line: bytes) -> None:
        try:
            record = json.loads(line)
        except ValueError:
            return
        payload = record.get("payload") if isinstance(record, dict) else None
        limits = payload.get("rate_limits") if isinstance(payload, dict) else None
        observed = _epoch(record.get("timestamp")) if isinstance(record, dict) else None
        if not isinstance(limits, dict) or observed is None:
            return
        limit_id = str(limits.get("limit_id") or "unknown")
        for slot in ("primary", "secondary"):
            window = limits.get(slot)
            used = _number(window.get("used_percent")) if isinstance(window, dict) else None
            if used is None:
                continue
            minutes = window.get("window_minutes")
            name = WINDOW_NAMES.get(minutes, f"{minutes}_minutes")
            self.points.setdefault((limit_id, name), []).append(
                (observed, float(used), _number(window.get("resets_at"))))

    def series(self, limit_id: str, name: str, resets_at=None, window_minutes=None) -> list[tuple[float, float]]:
        """``(observed_at, used_percent)`` pairs for one window, oldest first."""
        points = self.points.get((limit_id, name), [])
        if resets_at is not None:
            start = resets_at - (window_minutes or 0) * 60
            points = [p for p in points if p[0] >= start
                      and (p[2] is None or abs(p[2] - resets_at) <= RESET_TOLERANCE)]
        latest = {}
        for observed, used, _ in points:
            latest[observed] = used
        return sorted(latest.items())


def burn_rate(series, now: float, lookback_seconds: float) -> float | None:
    """Least-squares usage slope in percent per hour, or None without enough data."""
    recent = [(t, u) for t, u in series if t >= now - lookback_seconds]
    if len(recent) < 2 or recent[-1][0] - recent[0][0] < 600:
        return None
    count = len(recent)
    mean_t = sum(t for t, _ in recent) / count
    mean_u = sum(u for _, u in recent) / count
    spread = sum((t - mean_t) ** 2 for t, _ in recent)
    if spread == 0:
        return None
    slope = sum((t - mean_t) * (u - mean_u) for t, u in recent) / spread
    return round(slope * 3600, 3)


def projection(used, rate, now: float, resets_at=None) -> dict | None:
    """When the window runs out at the current burn rate, and whether that's before its reset."""
    if used is None or rate is None:
        return None
    if used >= 100:
        return {"exhausts_at": round(now), "exhausts_in_seconds": 0, "exhausts_before_reset": True}
    if rate <= 0:
        return {"exhausts_at": None, "exhausts_in_seconds": None, "exhausts_before_reset": False}
    at = now + (100 - used) / rate * 3600
    return {"exhausts_at": round(at), "exhausts_in_seconds": round(at - now),
            "exhausts_before_reset": resets_at is None or at < resets_at}


def downsample(series, max_points: int):
    if max_points < 2 or len(series) <= max_points:
        return list(series)
    step = (len(series) - 1) / (max_points - 1)
    return [series[round(index * step)] for index in range(max_points)]


def companion_events(pane, *, now: float, home=None, days: int = 8, limit: int = 50) -> list[dict]:
    """Recent companion rejections (newest first): the only limit history it leaves locally."""
    session = (pane or {}).get("coding_session") if isinstance(pane, dict) else None
    session_id = session.get("session_id") if isinstance(session, dict) else None
    events = {}
    for path in recent_companion_transcripts(home, session_id=session_id):
        for line in tail_lines(path, b'"quotaLimits"', limit=50):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            quota = _find(record, "quotaLimits")
            observed = _epoch(record.get("timestamp")) if isinstance(record, dict) else None
            if not isinstance(quota, dict) or observed is None or observed < now - days * 86400:
                continue
            kind = str(quota.get("rateLimitType") or "unknown")
            events[(observed, kind)] = {"observed_at": observed, "limit_type": kind,
                                        "status": quota.get("status"),
                                        "resets_at": _number(quota.get("resetsAt"))}
    return sorted(events.values(), key=lambda event: event["observed_at"], reverse=True)[:limit]


def _add_trend(window: dict, name: str, series, now: float, max_points: int) -> None:
    rate = burn_rate(series, now, LOOKBACK_SECONDS.get(name, 6 * 3600))
    window["history"] = downsample(series, max_points)
    window["burn_percent_per_hour"] = rate
    minutes, resets = window.get("window_minutes"), window.get("resets_at")
    elapsed = (now - (resets - minutes * 60)) / 3600 if isinstance(minutes, int) and resets else None
    # Pace since the window opened: still meaningful while the agent is idle.
    window["average_percent_per_hour"] = (round(window["used_percent"] / elapsed, 3)
                                          if elapsed and elapsed > 0 else None)
    window["projection"] = projection(window["used_percent"], rate, now, resets)


def limit_report(pane, text=None, *, now=None, low_percent: float = 10.0, history=None,
                 codex_home=None, companion_home=None, grok_home=None, omp_home=None,
                 max_points: int = 240) -> dict:
    """``assess_limits`` plus history, burn rate and projected exhaustion per window."""
    now = time.time() if now is None else now
    report = assess_limits(pane, text, now=now, low_percent=low_percent, codex_home=codex_home,
                           companion_home=companion_home, grok_home=grok_home, omp_home=omp_home)
    if report["agent"] == CODEX_ID and report.get("limit_id"):
        history = history if history is not None else CodexHistory(codex_home).refresh(now)
        for name, window in report["windows"].items():
            if window.get("source") != "codex rollout" or window.get("used_percent") is None:
                continue
            series = history.series(report["limit_id"], name, window.get("resets_at"),
                                    window.get("window_minutes"))
            _add_trend(window, name, series, now, max_points)
    elif report["agent"] == COMPANION_ID:
        report["events"] = companion_events(pane, now=now, home=companion_home)
    elif report["agent"] == GROK_ID:
        for name, window in report["windows"].items():
            if window.get("source") == "grok log" and window.get("used_percent") is not None:
                series = grok_series(grok_home, window.get("resets_at"), window.get("window_minutes"))
                _add_trend(window, name, series, now, max_points)
    return report


def _duration(seconds) -> str:
    if seconds is None:
        return "unknown"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return f"{days}d {hours}h" if days else f"{hours}h {minutes}m" if hours else f"{minutes}m"


def assess_limits(pane, text=None, *, now=None, low_percent: float = 10.0,
                  codex_home=None, companion_home=None, grok_home=None, omp_home=None) -> dict:
    """Return ``{"agent", "state", "reason", "windows", "not_reported", "sources", ...}``."""
    now = time.time() if now is None else now
    agent, session = pane_agent(pane, omp_home=omp_home)
    result = {"agent": agent, "state": "unknown", "reason": "", "windows": {},
              "not_reported": [], "sources": []}
    if agent not in LIMIT_PROVIDERS:
        result["reason"] = f"not a {', '.join(LIMIT_PROVIDERS)} pane"
        return result
    for key in ("detected_from", "model"):
        if session.get(key):
            result[key] = session[key]
    gather = {CODEX_ID: lambda: codex_evidence(session, now=now, home=codex_home),
              COMPANION_ID: lambda: companion_evidence(session, now=now, home=companion_home),
              GROK_ID: lambda: grok_evidence(now=now, home=grok_home),
              QWEN_ID: lambda: qwen_evidence(session, now=now, home=omp_home)}[agent]
    try:
        files = gather()
    except OSError as error:
        files, result["file_error"] = None, str(error)
    screen = screen_evidence(text) if text else None
    if files:
        result.update({key: value for key, value in files.items() if key not in ("windows", "source")})
        result["windows"] = dict(files["windows"])
        result["sources"].append(files["source"])
    if screen:
        result["sources"].append("screen")
        result["screen"] = screen
        for name, hint in screen["windows"].items():
            result["windows"].setdefault(name, {"used_percent": None, "remaining_percent": None,
                                                **hint, "source": "screen"})
    windows = result["windows"]
    result["not_reported"] = [name for name in ("weekly", "five_hour") if name not in windows]

    live = {name: w for name, w in windows.items() if not w.get("reset_since_observed")}
    exhausted = [name for name, w in live.items()
                 if (w.get("used_percent") or 0) >= 100 or w.get("status") == "rejected"]
    if files and files.get("reached") and live:
        exhausted.append(f"reached ({files['reached']})")
    last_success = result.get("last_success_at")
    fresh_files = bool(files and (
        (files.get("age_seconds") is not None and files["age_seconds"] < FRESH_SECONDS and live)
        or (last_success and now - last_success < FRESH_SECONDS)))
    if screen and screen["reached"] and not exhausted and not fresh_files:
        exhausted.append("screen reports the limit was hit (may be stale scrollback)")
    low = [name for name, w in live.items()
           if (w.get("remaining_percent") is not None and w["remaining_percent"] < low_percent)
           or (w.get("remaining_below_percent") is not None and w["remaining_below_percent"] <= low_percent)]

    details = []
    for name, w in windows.items():
        if w.get("used_percent") is not None:
            text_ = f"{name} {w['used_percent']:g}% used ({w['remaining_percent']:g}% left)"
        elif w.get("status"):
            text_ = f"{name} {w['status']}"
        else:
            text_ = f"{name} under {w.get('remaining_below_percent')}% left (screen)"
        if w.get("superseded_by_success_at"):
            text_ += ", a later request succeeded"
        elif w.get("reset_since_observed"):
            text_ += ", reset since observed"
        elif w.get("resets_in_seconds") is not None:
            text_ += f", resets in {_duration(w['resets_in_seconds'])}"
        details.append(text_)
    if result["not_reported"] and windows:
        details.append(" and ".join(result["not_reported"]) + " not reported")
    if last_success and not windows:
        details.append(f"no recent quota rejections; last successful request {_duration(now - last_success)} ago")

    if exhausted:
        result["state"] = "exhausted"
        result["exhausted"] = exhausted
    elif low:
        result["state"] = "low"
        result["low"] = low
    elif any(w.get("used_percent") is not None for w in live.values()):
        result["state"] = "ok"
    result["reason"] = "; ".join(details) or "no limit evidence found"
    return result
