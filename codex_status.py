#!/usr/bin/env python3
"""Codex account status from a standing tmux session, not the usage website.

The session is ``codex_usage``. ``tb a codex_usage`` would create it and then
attach, and attach needs a TTY and never returns, so the CLI creates it with
``tb new`` (the same create) and leaves it detached. A missing session is
detected from ``tb ls``, matching the session name exactly.

Codex is started in that session when the pane is still a shell. Startup
update checks are turned off for that launch: on this machine a plain
``codex`` tries to upgrade itself, fails for lack of permission, and exits.
``/status`` is submitted twice, and only after the foreground is still Codex.
The screen dumped after the second submission is the reading; the first is
discarded.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time

from usage_pages import window

SESSION = "codex_usage"
# A plain `codex` here runs `npm install -g` on startup and exits when that
# cannot write /usr/local. The TUI never comes up, so the status launch skips it.
CODEX_LAUNCH = "codex -c check_for_update_on_startup=false"
SHELLS = frozenset({"bash", "sh", "zsh", "fish", "dash", "nu", "login"})
_READY_TIMEOUT = 60.0
_STATUS_TIMEOUT = 15.0
_PERCENT = re.compile(
    r"(weekly|5-hour|5h|five[- ]hour|context(?: window)?|primary|secondary)"
    r"(?: limit)?[^%\n]{0,48}?"
    r"(\d+(?:\.\d+)?)\s*%\s*(left|used)",
    re.I,
)
_NAMES = {
    "weekly": "weekly", "5-hour": "five_hour", "5h": "five_hour",
    "five-hour": "five_hour", "five hour": "five_hour",
    "primary": "five_hour", "secondary": "weekly",
    "context": "context", "context window": "context",
}
_SHELL_PROMPT = re.compile(r"[$%#]\s*$")


class CodexStatusError(Exception):
    pass


def session_listed(text: str, name: str = SESSION) -> bool:
    """True when ``tb ls`` output names this session. The name must match a whole field."""
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] == name:
            return True
    return False


def screen_ready(text: str) -> bool:
    """The Codex composer is up and is not busy, still a shell, or waiting on trust."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    lowered = text.lower()
    if "esc to interrupt" in lowered or "do you trust" in lowered:
        return False
    if "updating codex" in lowered or "npm err!" in lowered or "npm install -g" in lowered:
        return False
    if _SHELL_PROMPT.search(lines[-1]):
        return False
    return "›" in text or "/status" in lowered or "context" in lowered or "gpt-" in lowered


def is_codex_command(command: str) -> bool:
    return bool(re.search(r"(?:^|[ /])codex(?:\.js)?(?:\s|$)", command or ""))


_RESET = re.compile(r"resets\s+(\d{1,2}):(\d{2})\s*([AP]M)\s+on\s+(\d{1,2})\s+([A-Za-z]{3})", re.I)
_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _reset_epoch(line: str, now: float) -> float | None:
    match = _RESET.search(line)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == "pm":
        hour += 12
    minute = int(match.group(2))
    day = int(match.group(4))
    month = _MONTHS.get(match.group(5).lower())
    if month is None:
        return None
    year = time.localtime(now).tm_year
    stamp = time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))
    if stamp < now - 86400:
        stamp = time.mktime((year + 1, month, day, hour, minute, 0, 0, 0, -1))
    return stamp


def latest_status(screen: str) -> str:
    """The last status card in a capture. Earlier ``/status`` output stays in scrollback."""
    lines = screen.splitlines()
    banners = [index for index, line in enumerate(lines) if ">_ OpenAI Codex" in line]
    if not banners:
        return screen
    return "\n".join(lines[banners[-1]:]).strip("\n") + "\n"


def composer_contains(text: str, line: str) -> bool:
    """True when the Codex composer, the line beginning with ›, holds this command."""
    return any("›" in raw and line in raw for raw in text.splitlines())


def parse_limits(screen: str, now: float | None = None) -> tuple[list[dict], float | None]:
    """Account windows, plus the context-window used percent when the screen states one.

    Context is not an account window: a full context does not mean the account is exhausted.
    ``primary`` is the five-hour window and ``secondary`` is the weekly window. An explicit
    five-hour or weekly label wins over those names.
    """
    now = time.time() if now is None else now
    explicit: dict[str, tuple[float, float | None]] = {}
    guessed: dict[str, tuple[float, float | None]] = {}
    context = None
    for match in _PERCENT.finditer(screen):
        kind = match.group(1).lower()
        number = float(match.group(2))
        used = number if match.group(3).lower() == "used" else round(max(0.0, 100 - number), 1)
        line_end = screen.find("\n", match.end())
        line = screen[screen.rfind("\n", 0, match.start()) + 1:len(screen) if line_end < 0 else line_end]
        resets = _reset_epoch(line, now)
        name = _NAMES[kind]
        if name == "context":
            context = used
        elif kind in ("primary", "secondary"):
            guessed.setdefault(name, (used, resets))
        else:
            explicit[name] = (used, resets)  # the later block is the second /status
    found = {**guessed, **explicit}
    order = [name for name in ("five_hour", "weekly") if name in found]
    return [window(name, found[name][0], resets=found[name][1]) for name in order], context


def _run(args: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["tb", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as error:
        raise CodexStatusError("tb is not installed") from error
    except subprocess.TimeoutExpired as error:
        raise CodexStatusError(f"tb {' '.join(args)} timed out") from error


def _ok(result: subprocess.CompletedProcess, what: str) -> str:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise CodexStatusError(detail or f"tb {what} failed ({result.returncode})")
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
    return latest


def _excerpt(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-12:])


def descendant_commands(pid: int) -> list[str]:
    """Command lines of this process and its descendants, from /proc."""
    parents: dict[int, list[int]] = {}
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        tail = stat.rsplit(")", 1)[-1].split()
        if len(tail) < 2:
            continue
        parents.setdefault(int(tail[1]), []).append(int(entry.name))

    def command(process: int) -> str:
        try:
            raw = (proc / str(process) / "cmdline").read_bytes()
        except OSError:
            return ""
        return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()

    commands = [command(pid)]
    stack = list(parents.get(pid, []))
    while stack:
        child = stack.pop()
        commands.append(command(child))
        stack.extend(parents.get(child, []))
    return commands


def _pane_runs_codex(pane: dict, commands_for) -> bool:
    if pane.get("command") == "codex":
        return True
    pid = pane.get("pid")
    if not isinstance(pid, int):
        return False
    return any(is_codex_command(command) for command in commands_for(pid))


def read(*, cwd: str | None = None, run=None, commands_for=None,
         clock=None, sleep=None, now: float | None = None) -> dict:
    """Read Codex /status. Failures become an error entry so the other agents still report."""
    try:
        return _read(cwd=cwd, run=run or _run, commands_for=commands_for or descendant_commands,
                     clock=clock or time.monotonic, sleep=sleep or time.sleep, now=now)
    except CodexStatusError as error:
        return {"status": "error", "reason": str(error), "checked_at": time.time() if now is None else now,
                "windows": [], "source": f"tmux {SESSION} /status"}


def _read(*, cwd, run, commands_for, clock, sleep, now) -> dict:
    now = time.time() if now is None else now
    cwd = cwd or os.getcwd()
    listed = run(["ls", "--no-header"])
    if listed.returncode not in (0, 6):
        _ok(listed, "ls")
    if listed.returncode == 6 or not session_listed(listed.stdout):
        created = run(["new", SESSION, "--cwd", cwd])
        if created.returncode not in (0, 4):
            _ok(created, "new")
    shown = json.loads(_ok(run(["show", "--json", SESSION]), "show") or "{}")
    panes = (shown.get("data") or {}).get("panes") or []
    pane = next((item for item in panes if item.get("active")), None) or (panes[:1] or [None])[0]
    if not isinstance(pane, dict):
        raise CodexStatusError(f"tmux session {SESSION} has no pane")
    if not _pane_runs_codex(pane, commands_for):
        command = str(pane.get("command") or "")
        if command not in SHELLS:
            raise CodexStatusError(
                f"{SESSION} is running {command or 'an unknown program'}; leave it at a shell or at codex")
        _ok(run(["type", SESSION, CODEX_LAUNCH]), "type")

    def capture() -> str:
        return _ok(run(["capture", "-n", "80", SESSION]), "capture")

    def pane_is_codex() -> bool:
        shown = json.loads(_ok(run(["show", "--json", SESSION]), "show") or "{}")
        panes = (shown.get("data") or {}).get("panes") or []
        current = next((item for item in panes if item.get("active")), None) or (panes[:1] or [None])[0]
        return isinstance(current, dict) and _pane_runs_codex(current, commands_for)

    def ready_screen(text: str) -> bool:
        return screen_ready(text) and pane_is_codex()

    ready = _wait(capture, ready_screen, _READY_TIMEOUT, clock=clock, sleep=sleep)
    if not screen_ready(ready):
        if "do you trust" in ready.lower():
            raise CodexStatusError(f"codex is waiting for a directory trust decision in {SESSION}")
        raise CodexStatusError(f"codex in {SESSION} did not become ready\n{_excerpt(ready)}")
    def submit(line: str) -> None:
        # Text and Enter go separately. Sending them together lets the open
        # status card take the Enter, and the command sits in the composer.
        _ok(run(["send", SESSION, line]), "send")
        shown = _wait(capture, lambda text: composer_contains(text, line), _STATUS_TIMEOUT,
                      clock=clock, sleep=sleep)
        if not composer_contains(shown, line):
            raise CodexStatusError(f"{line} did not reach the composer in {SESSION}\n{_excerpt(shown)}")
        _ok(run(["key", SESSION, "Enter"]), "key")

    def after_submit(previous: str) -> str:
        started = clock()

        def landed(text: str) -> bool:
            if composer_contains(text, "/status"):
                return False
            return _norm(text) != _norm(previous) or clock() - started >= 1.5

        return _wait(capture, landed, _STATUS_TIMEOUT, clock=clock, sleep=sleep)

    submit("/status")
    first = after_submit(ready)
    submit("/status")
    screen = after_submit(first)
    screen = latest_status(screen)
    windows, context = parse_limits(screen, now)
    entry = {"status": "ok", "checked_at": now, "source": f"tmux {SESSION} /status",
             "session": SESSION, "screen": screen, "windows": windows}
    if context is not None:
        entry["context_used_percent"] = context
    return entry
