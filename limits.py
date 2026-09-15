#!/usr/bin/env python3
"""Token-limit detection for coding session panes: weekly and five-hour windows.

Limits belong to the account, not to one pane. Evidence therefore comes from
the pane's own session file and the most recently written session files of the
same agent.

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
Screen
    Warnings such as "less than 5% of your weekly limit left" and "hit your
    usage limit", matched only at the start of a line so quoted text elsewhere
    doesn't count. Scrollback can be stale, so fresh file evidence outranks it.

``state`` is ``exhausted``, ``low``, ``ok`` or ``unknown``. Unknown never blocks.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import time

from providers import (CODEX_HOME_DIR, CODEX_HOME_ENV, CODEX_ID, COMPANION_CONFIG_ENV,
                       COMPANION_HOME_DIR, COMPANION_ID, SUPPORTED)

WINDOW_NAMES = {300: "five_hour", 10080: "weekly"}
COMPANION_WINDOWS = {"five_hour": "five_hour", "seven_day": "weekly"}
FRESH_SECONDS = 3600

_SESSION_ID = re.compile(r"[0-9A-Za-z-]{8,64}")
_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_DUST = re.compile("[⠀-⣿]")
_SCREEN_LOW = re.compile(
    r"^\W*(?:heads up,\s*)?you have less than (\d{1,3})% of your "
    r"(weekly|5h|5-hour|five-hour) limit left", re.I)
_SCREEN_REACHED = re.compile(
    r"^\W*(?:(?:you've|you have)\s+)?hit your (?:usage )?limit|^\W*(?:\w+ )?usage limit reached", re.I)
_SCREEN_NAMES = {"weekly": "weekly", "5h": "five_hour", "5-hour": "five_hour", "five-hour": "five_hour"}


def codex_home(value=None) -> Path:
    return Path(value or os.environ.get(CODEX_HOME_ENV) or Path.home() / CODEX_HOME_DIR)


def companion_home(value=None) -> Path:
    return Path(value or os.environ.get(COMPANION_CONFIG_ENV) or Path.home() / COMPANION_HOME_DIR)


def _epoch(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


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
    """The most recently written rollouts, found newest day first."""
    candidates: list[Path] = []
    for year in _subdirs(codex_home(home) / "sessions"):
        for month in _subdirs(year):
            for day in _subdirs(month):
                candidates.extend(day.glob("rollout-*.jsonl"))
                if len(candidates) >= count * 4:
                    return sorted(candidates, key=_mtime, reverse=True)[:count]
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


def limit_report(pane, text=None, *, now=None, low_percent: float = 10.0, history=None,
                 codex_home=None, companion_home=None, max_points: int = 240) -> dict:
    """``assess_limits`` plus history, burn rate and projected exhaustion per window."""
    now = time.time() if now is None else now
    report = assess_limits(pane, text, now=now, low_percent=low_percent,
                           codex_home=codex_home, companion_home=companion_home)
    if report["agent"] == CODEX_ID and report.get("limit_id"):
        history = history if history is not None else CodexHistory(codex_home).refresh(now)
        for name, window in report["windows"].items():
            if window.get("source") != "codex rollout" or window.get("used_percent") is None:
                continue
            series = history.series(report["limit_id"], name, window.get("resets_at"),
                                    window.get("window_minutes"))
            rate = burn_rate(series, now, LOOKBACK_SECONDS.get(name, 6 * 3600))
            window["history"] = downsample(series, max_points)
            window["burn_percent_per_hour"] = rate
            minutes, resets = window.get("window_minutes"), window.get("resets_at")
            elapsed = (now - (resets - minutes * 60)) / 3600 if isinstance(minutes, int) and resets else None
            # Pace since the window opened: still meaningful while the agent is idle.
            window["average_percent_per_hour"] = (round(window["used_percent"] / elapsed, 3)
                                                  if elapsed and elapsed > 0 else None)
            window["projection"] = projection(window["used_percent"], rate, now, window.get("resets_at"))
    elif report["agent"] == COMPANION_ID:
        report["events"] = companion_events(pane, now=now, home=companion_home)
    return report


def _duration(seconds) -> str:
    if seconds is None:
        return "unknown"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return f"{days}d {hours}h" if days else f"{hours}h {minutes}m" if hours else f"{minutes}m"


def assess_limits(pane, text=None, *, now=None, low_percent: float = 10.0,
                  codex_home=None, companion_home=None) -> dict:
    """Return ``{"agent", "state", "reason", "windows", "not_reported", "sources", ...}``."""
    now = time.time() if now is None else now
    session = (pane or {}).get("coding_session") if isinstance(pane, dict) else None
    session = session if isinstance(session, dict) else {}
    agent = str(session.get("provider") or "")
    result = {"agent": agent, "state": "unknown", "reason": "", "windows": {},
              "not_reported": [], "sources": []}
    if agent not in SUPPORTED:
        result["reason"] = f"not a {CODEX_ID} or {COMPANION_ID} pane"
        return result
    try:
        files = (codex_evidence(session, now=now, home=codex_home) if agent == CODEX_ID
                 else companion_evidence(session, now=now, home=companion_home))
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
    fresh_files = bool(files and files.get("age_seconds") is not None
                       and files["age_seconds"] < FRESH_SECONDS and live)
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
        if w.get("reset_since_observed"):
            text_ += ", reset since observed"
        elif w.get("resets_in_seconds") is not None:
            text_ += f", resets in {_duration(w['resets_in_seconds'])}"
        details.append(text_)
    if result["not_reported"] and windows:
        details.append(" and ".join(result["not_reported"]) + " not reported")

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
