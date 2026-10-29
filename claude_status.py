#!/usr/bin/env python3
"""Claude account status from a standing tmux session, not the usage website.

The session is ``claude_usage``. ``tb a claude_usage`` would create it and then
attach, and attach needs a TTY and never returns, so the CLI creates it with
``tb new`` and leaves it detached. A missing session is detected from ``tb ls``,
matching the session name exactly.

Claude is started there when the pane is still a shell. After it is up, ``/status``
is submitted and the right arrow is pressed twice: the dialog opens on Status,
and two pages over is Usage. When that dialog is already open, the reading is
refreshed with the left arrow, then the right arrow, and the screen is dumped
a second later. ``/status`` is not typed again in that case.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import re
import subprocess
import time

from codex_status import SHELLS, descendant_commands, session_listed
from usage_pages import window
from idleness import composer_text
from status_session import StatusSessionError, Transport, reader_lock

SESSION = "claude_usage"
CLAUDE_LAUNCH = "claude"
_READY_TIMEOUT = 60.0
_STATUS_TIMEOUT = 15.0
_LAUNCH_WAIT = 2.0
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(used|left)", re.I)
_RESET = re.compile(
    r"resets\s+(?:(?P<mon>[A-Za-z]{3,9})\s+(?P<day>\d{1,2}),?\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]m)"
    r"(?:\s*\((?P<tz>[^)]+)\))?",
    re.I,
)
_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_BAR = re.compile(r"[█░▓▒▌\s]+")
_SHELL_PROMPT = re.compile(r"[$%#]\s*$")


class ClaudeStatusError(Exception):
    pass


def is_claude_command(command: str) -> bool:
    return bool(re.search(r"(?:^|[ /])claude(?:\s|$)", command or ""))


def screen_ready(text: str) -> bool:
    """The Claude composer is up and is not a shell or a trust prompt."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    lowered = text.lower()
    if "do you trust" in lowered or "esc to interrupt" in lowered:
        return False
    if _SHELL_PROMPT.search(lines[-1]):
        return False
    return "❯" in text


def status_open(text: str) -> bool:
    """The /status dialog is the visible page, not an old copy in scrollback."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    tail = "\n".join(lines[-40:]).lower()
    return (any("esc to cancel" in line.lower() for line in lines[-3:])
            and "settings" in tail and "usage" in tail
            and not any(line.startswith("❯") for line in lines[-3:]))


def composer_contains(text: str, line: str) -> bool:
    """True when the Claude composer, the line containing ❯, holds this command."""
    return composer_text("claude", text) == line


def latest_page(screen: str) -> str:
    """The last /status page in a capture. Earlier pages stay in scrollback."""
    lines = screen.splitlines()
    headers = [index for index, line in enumerate(lines)
               if "Settings" in line and "Status" in line and "Usage" in line]
    if not headers:
        return screen
    return "\n".join(lines[headers[-1]:]).strip("\n") + "\n"


def _zone(name: str | None):
    if not name or not name.strip():
        return None
    try:
        return ZoneInfo(name.strip())
    except Exception:
        return None


def _reset_epoch(line: str, now: float) -> float | None:
    match = _RESET.search(line)
    if not match:
        return None
    hour = int(match.group("hour")) % 12
    if match.group("ampm").lower() == "pm":
        hour += 12
    minute = int(match.group("minute") or 0)
    month_name = (match.group("mon") or "").lower()[:3]
    month = _MONTHS.get(month_name)
    zone = _zone(match.group("tz"))
    if zone is not None:
        current = datetime.fromtimestamp(now, zone)
        if month:
            stamp = datetime(current.year, month, int(match.group("day")), hour, minute, tzinfo=zone)
            if stamp.timestamp() < now - 86400:
                stamp = stamp.replace(year=current.year + 1)
        else:
            stamp = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if stamp.timestamp() < now - 60:
                stamp += timedelta(days=1)
        return stamp.timestamp()
    year = time.localtime(now).tm_year
    if month:
        stamp = time.mktime((year, month, int(match.group("day")), hour, minute, 0, 0, 0, -1))
        if stamp < now - 86400:
            stamp = time.mktime((year + 1, month, int(match.group("day")), hour, minute, 0, 0, 0, -1))
        return stamp
    local = time.localtime(now)
    stamp = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, hour, minute, 0, 0, 0, -1))
    if stamp < now - 60:
        stamp += 86400
    return stamp


def _label_before(lines: list[str], index: int) -> str:
    for line in reversed(lines[:index]):
        text = line.strip()
        if not text or _BAR.fullmatch(text):
            continue
        return text
    return ""


def _window_for(label: str) -> tuple[str, int, str] | None:
    raw = label.strip()
    folded = raw.lower()
    if folded == "current session":
        return "five_hour", 5 * 3600, "5 hour"
    match = re.fullmatch(r"current week \((.+)\)", folded)
    if not match:
        return None
    inner = match.group(1).strip()
    if inner in {"all models", "all"}:
        return "weekly", 7 * 86400, "weekly"
    slug = re.sub(r"[^a-z0-9]+", "_", inner).strip("_")
    title = raw[raw.find("(") + 1:raw.rfind(")")].strip()
    return f"weekly_{slug}", 7 * 86400, f"weekly ({title})"


def parse_limits(screen: str, now: float | None = None) -> list[dict]:
    """Usage-page windows. A context or session token count is not an account window.

    ``Current session`` is the five-hour window. ``Current week (all models)`` is
    the weekly window. Any other ``Current week (...)`` line is that model's week.
    """
    now = time.time() if now is None else now
    lines = screen.splitlines()
    found: dict[str, dict] = {}
    order: list[str] = []
    for index, line in enumerate(lines):
        match = _PERCENT.search(line)
        if not match:
            continue
        spec = _window_for(_label_before(lines, index))
        if spec is None:
            continue
        name, seconds, label = spec
        number = float(match.group(1))
        used = number if match.group(2).lower() == "used" else round(max(0.0, 100 - number), 1)
        resets = None
        for follower in lines[index + 1:index + 4]:
            if not follower.strip():
                continue
            resets = _reset_epoch(follower, now)
            break
        if name not in found:
            order.append(name)
        found[name] = window(name, used, resets, seconds=seconds, label=label)
    head = [name for name in ("five_hour", "weekly") if name in found]
    rest = [name for name in order if name not in head]
    return [found[name] for name in head + rest]


def _run(args: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["tb", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as error:
        raise ClaudeStatusError("tb is not installed") from error
    except subprocess.TimeoutExpired as error:
        raise ClaudeStatusError(f"tb {' '.join(args)} timed out") from error


def _ok(result: subprocess.CompletedProcess, what: str) -> str:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ClaudeStatusError(detail or f"tb {what} failed ({result.returncode})")
    return result.stdout


def _norm(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _wait(capture, predicate, timeout: float, *, clock, sleep, interval: float = 0.4) -> str:
    deadline = clock() + timeout
    last = None
    stable = 0
    latest = ""
    while clock() < deadline:
        latest = capture()
        stable = stable + 1 if _norm(latest) == last else 1
        last = _norm(latest)
        if stable >= 2 and predicate(latest):
            return latest
        sleep(interval)
    raise ClaudeStatusError("timed out waiting for the status pane to become ready")


def _excerpt(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-12:])


def _pane_runs_claude(pane: dict, commands_for) -> bool:
    if pane.get("command") == "claude":
        return True
    pid = pane.get("pid")
    if not isinstance(pid, int):
        return False
    return any(is_claude_command(command) for command in commands_for(pid))


def read(*, cwd: str | None = None, run=None, commands_for=None,
         clock=None, sleep=None, now: float | None = None) -> dict:
    """Read Claude /status. Failures become an error entry so the other agents still report."""
    try:
        commands_for = commands_for or descendant_commands
        with reader_lock(SESSION):
            transport = Transport(run or _run, SESSION, "claude",
                                  lambda pane: _pane_runs_claude(pane, commands_for))
            return _read(cwd=cwd, run=transport, commands_for=commands_for,
                         clock=clock or time.monotonic, sleep=sleep or time.sleep, now=now)
    except (ClaudeStatusError, StatusSessionError, OSError, ValueError, TypeError) as error:
        return {"status": "error", "reason": str(error), "checked_at": time.time() if now is None else now,
                "windows": [], "source": f"tmux {SESSION} /status"}


def _read(*, cwd, run, commands_for, clock, sleep, now) -> dict:
    now = time.time() if now is None else now
    cwd = cwd or os.getcwd()
    listed = run(["ls", "--no-header"])
    if listed.returncode not in (0, 6):
        _ok(listed, "ls")
    if listed.returncode == 6 or not session_listed(listed.stdout, SESSION):
        created = run(["new", SESSION, "--cwd", cwd])
        if created.returncode not in (0, 4):
            _ok(created, "new")
    shown = json.loads(_ok(run(["show", "--json", SESSION]), "show") or "{}")
    panes = (shown.get("data") or {}).get("panes") or []
    pane = next((item for item in panes if item.get("active")), None) or (panes[:1] or [None])[0]
    if not isinstance(pane, dict):
        raise ClaudeStatusError(f"tmux session {SESSION} has no pane")

    def capture() -> str:
        return _ok(run(["capture", "-n", "80", SESSION]), "capture")

    def pane_is_claude() -> bool:
        shown = json.loads(_ok(run(["show", "--json", SESSION]), "show") or "{}")
        panes = (shown.get("data") or {}).get("panes") or []
        current = next((item for item in panes if item.get("active")), None) or (panes[:1] or [None])[0]
        return isinstance(current, dict) and _pane_runs_claude(current, commands_for)

    def open_status() -> None:
        # Text and Enter go separately. The dialog can take a bundled Enter.
        _ok(run(["send", SESSION, "/status"]), "send")
        shown = _wait(capture, lambda text: composer_contains(text, "/status"), _STATUS_TIMEOUT,
                      clock=clock, sleep=sleep)
        if not composer_contains(shown, "/status"):
            raise ClaudeStatusError(f"/status did not reach the composer in {SESSION}\n{_excerpt(shown)}")
        _ok(run(["key", SESSION, "Enter"]), "key")
        opened = _wait(capture, status_open, _STATUS_TIMEOUT, clock=clock, sleep=sleep)
        if not status_open(opened):
            raise ClaudeStatusError(f"/status did not open in {SESSION}\n{_excerpt(opened)}")
        # Status is the open page. Two pages to the right is Usage.
        _ok(run(["key", SESSION, "Right"]), "key")
        _ok(run(["key", SESSION, "Right"]), "key")

    if not _pane_runs_claude(pane, commands_for):
        command = str(pane.get("command") or "")
        if command not in SHELLS:
            raise ClaudeStatusError(
                f"{SESSION} is running {command or 'an unknown program'}; leave it at a shell or at claude")
        _ok(run(["type", SESSION, CLAUDE_LAUNCH]), "type")
        launched = clock()

        def ready(text: str) -> bool:
            return clock() - launched >= _LAUNCH_WAIT and screen_ready(text) and pane_is_claude()

        ready_text = _wait(capture, ready, _READY_TIMEOUT, clock=clock, sleep=sleep)
        if not screen_ready(ready_text):
            if "do you trust" in ready_text.lower():
                raise ClaudeStatusError(f"claude is waiting for a directory trust decision in {SESSION}")
            raise ClaudeStatusError(f"claude in {SESSION} did not become ready\n{_excerpt(ready_text)}")
        open_status()
    else:
        current = capture()
        if status_open(current):
            # Already on /status. Left, then right, redraws the current page.
            _ok(run(["key", SESSION, "Left"]), "key")
            _ok(run(["key", SESSION, "Right"]), "key")
            sleep(1)
        else:
            if not screen_ready(current):
                current = _wait(capture, lambda text: screen_ready(text) and pane_is_claude(),
                                _READY_TIMEOUT, clock=clock, sleep=sleep)
            if not screen_ready(current):
                raise ClaudeStatusError(f"claude in {SESSION} did not become ready\n{_excerpt(current)}")
            open_status()

    def usage_visible(text: str) -> bool:
        return "current session" in text.lower() and "%" in text

    screen = latest_page(_wait(capture, usage_visible, _STATUS_TIMEOUT, clock=clock, sleep=sleep))
    windows = parse_limits(screen, now)
    if not windows:
        raise ClaudeStatusError(f"claude /status in {SESSION} did not show usage\n{_excerpt(screen)}")
    return {"status": "ok", "checked_at": now, "source": f"tmux {SESSION} /status",
            "session": SESSION, "screen": screen, "windows": windows}
