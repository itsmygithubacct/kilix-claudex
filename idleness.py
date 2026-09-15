#!/usr/bin/env python3
"""Decide whether a coding agent pane in kilix is ready to receive a prompt.

``assess_idle(pane, text)`` is pure. ``pane`` is one record from
``kilix-panes list --json`` (schema ``kilix.panes/v1``). ``text`` is a screen
capture from ``kilix @ get-text --extent screen``, preferably taken with
``--ansi`` so faint placeholder text can be told apart from typed text.

It weighs two kinds of evidence, strongest first:

1. The pane record's ``activity``. Pane Center derives it from the agent's own
   session log: codex rollout turn boundaries, or the companion's live session status.
2. The live UI at the bottom of the screen: the busy footer, queued input,
   dialogs, the feedback survey, and whether the input box already holds text.

A pane counts as ready only when neither source objects. Anything
unrecognised is reported as not ready. Skipping a tick costs little; a prompt
typed into a busy or occupied composer does real damage.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys

from providers import CODEX_ID, COMPANION_ID, COMPANION_SURVEY_TEXT, SUPPORTED


_BRAILLE = re.compile("[⠀⠁⠂⠄⠈⠐⠠⡀⢀]")  # blank/single-dot Kilix dust; preserve braille letters
_ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_SGR = re.compile(r"\x1b\[([0-9;:]*)m")

_DEAD = re.compile(
    r"hit your (?:usage )?limit|usage limit reached|out of credits", re.I)
_BUSY = re.compile(r"esc to interrupt", re.I)
_DIALOG = re.compile(
    r"esc to cancel|do you want to proceed|would you like to run"
    r"|enter to select|submit with unanswered", re.I)

_CODEX_PLACEHOLDERS = ("Ask Codex to do anything", "Ask a follow-up question")
_CODEX_QUEUED = re.compile(r"Messages to be submitted|Queued follow-up", re.I)
_CODEX_SEPARATOR = re.compile(r"^─+ Worked for ")
_CODEX_WORKING = re.compile(r"^• Working \(")
_CODEX_LOOKBACK = 15  # lines above the composer that count as live UI

_COMPANION_QUEUED = "Press up to edit queued messages"
_COMPANION_SURVEY = re.compile(re.escape(COMPANION_SURVEY_TEXT) + r"|0: Dismiss")


# --- screen parsing -------------------------------------------------------

def _clean(s: str) -> str:
    return _BRAILLE.sub(" ", s).replace("\xa0", " ").rstrip()


def _apply_sgr(params: str, faint: bool) -> bool:
    parts = params.split(";") if params else ["0"]
    i = 0
    while i < len(parts):
        head = parts[i].split(":")[0]
        if head in ("", "0", "22"):
            faint = False
        elif head == "2":
            faint = True
        elif head in ("38", "48", "58") and ":" not in parts[i]:
            # 38;5;n and 38;2;r;g;b carry arguments that must not be read as
            # attributes; a bare "2" there is a colour mode, not faint.
            mode = parts[i + 1] if i + 1 < len(parts) else ""
            i += 2 if mode == "5" else 4 if mode == "2" else 0
        i += 1
    return faint


def _visible(line: str) -> tuple[str, str]:
    """Return ``(plain, typed)``: all visible text, and the non-faint part."""
    plain: list[str] = []
    typed: list[str] = []
    faint = False
    pos = 0
    for m in _ESCAPE.finditer(line):
        chunk = line[pos:m.start()]
        plain.append(chunk)
        if not faint:
            typed.append(chunk)
        sgr = _SGR.fullmatch(m.group(0))
        if sgr:
            faint = _apply_sgr(sgr.group(1), faint)
        pos = m.end()
    plain.append(line[pos:])
    if not faint:
        typed.append(line[pos:])
    return _clean("".join(plain)), _clean("".join(typed))


def _rows(text: str) -> list[tuple[str, str]]:
    rows = [_visible(line) for line in text.replace("\r", "").split("\n")]
    while rows and not rows[-1][0].strip():
        rows.pop()
    return rows


def _squash(parts) -> str:
    return " ".join(" ".join(parts).split())


def _last(items, pred) -> int | None:
    for i in range(len(items) - 1, -1, -1):
        if pred(items[i]):
            return i
    return None


def _codex_screen(rows, ansi: bool) -> tuple[str, str]:
    plain = [p for p, _ in rows]
    composer = _last(plain, lambda s: s.lstrip().startswith("›"))
    if composer is None:
        if any(_DIALOG.search(s) for s in plain[-_CODEX_LOOKBACK:]):
            return "waiting", "a codex dialog is open"
        return "no-composer", "no codex input line (›) on screen"

    # Live UI is what sits below the last finished turn, and never more than
    # a few lines above the composer. Transcript text further up can quote
    # any marker, this file's own strings included.
    sep = _last(plain[:composer],
                lambda s: bool(_CODEX_SEPARATOR.match(s.lstrip())))
    start = max(composer - _CODEX_LOOKBACK, 0 if sep is None else sep + 1)
    live = "\n".join(plain[start:])
    if _DEAD.search(live):
        return "dead", "codex reports a usage limit"

    # Check for input already waiting before checking busy. A working pane
    # can still hold someone's draft or queued text, and a caller queuing an
    # FYI into a busy pane has to see that. "working" alone would hide it.
    # The composer is the prompt line plus any continuation lines above the
    # status footer.
    body = rows[composer:-1] if len(rows) - composer > 1 else rows[composer:]
    pick = 1 if ansi else 0
    first = body[0][pick].lstrip().removeprefix("›")
    content = _squash([first] + [r[pick] for r in body[1:]])
    if content and (ansi or content not in _CODEX_PLACEHOLDERS):
        return "occupied", f"codex composer holds text: {content[:60]!r}"
    if _CODEX_QUEUED.search(live):
        return "queued", "codex already has queued input"
    if _DIALOG.search(live):
        return "waiting", "a codex dialog is open"
    if _BUSY.search(live) or any(
            _CODEX_WORKING.match(s.lstrip()) for s in plain[start:composer]):
        return "working", "codex busy indicator above the composer"
    return "idle", "codex composer is empty"


def codex_queue_hint(text: str) -> bool:
    """Whether the footer below the current composer advertises Tab to queue."""
    rows = _rows(text)
    composer = _last(rows, lambda row: row[0].lstrip().startswith("›"))
    if composer is None:
        return False
    footer = _squash(row[0] for row in rows[max(composer + 1, len(rows) - 2):])
    return "tab to queue message" in footer.lower()


def composer_text(agent: str, text: str) -> str | None:
    """Visible composer content, excluding transcript, footer and queue hint."""
    plain = [row[0] for row in _rows(text)]
    if agent == "codex":
        start = _last(plain, lambda line: line.lstrip().startswith("›"))
        if start is None or start == len(plain) - 1:
            return None
        body = plain[start:-1]
        body[0] = body[0].lstrip().removeprefix("›")
        body = [line for line in body if not line.strip().lower().startswith("tab to queue message")]
    elif agent == COMPANION_ID:
        rules = [i for i, line in enumerate(plain) if _is_rule(line)]
        if len(rules) < 2:
            return None
        top, bottom = rules[-2:]
        if bottom - top < 2 or not plain[top + 1].lstrip().startswith("❯"):
            return None
        body = plain[top + 1:bottom]
        body[0] = body[0].lstrip().removeprefix("❯")
    else:
        return None
    return "\n".join(body).strip()


def composer_is_empty(agent: str, text: str) -> bool:
    """True only when a composer is visible and holds nothing but its placeholder."""
    content = composer_text(agent, text)
    if content is None:
        return False
    return content == "" or (agent == "codex" and content in _CODEX_PLACEHOLDERS)


def _is_rule(s: str) -> bool:
    s = s.strip()
    return len(s) >= 10 and s.count("─") >= 0.8 * len(s)


def _companion_screen(rows, ansi: bool) -> tuple[str, str]:
    plain = [p for p, _ in rows]
    tail = "\n".join(plain[-20:])
    if _DEAD.search(tail):
        return "dead", "companion reports a usage limit"

    rules = [i for i, s in enumerate(plain) if _is_rule(s)]
    top = bottom = None
    if len(rules) >= 2:
        t, b = rules[-2], rules[-1]
        if b - t >= 2 and plain[t + 1].lstrip().startswith("❯"):
            top, bottom = t, b
    if top is None:
        if _DIALOG.search(tail):
            return "waiting", "a companion dialog or menu replaces the input box"
        return "no-composer", "no companion input box (❯ between rules) on screen"

    # Pending input comes before busy, as in _codex_screen. The queued hint
    # lives inside the box, so check it before treating box text as a draft.
    box = rows[top + 1:bottom]
    shown = _squash(p for p, _ in box).removeprefix("❯").strip()
    if _COMPANION_QUEUED in shown:
        return "queued", "companion already has queued input"
    content = (_squash(t for _, t in box).removeprefix("❯").strip()
               if ansi else shown)
    if content:
        return "occupied", f"companion input box holds text: {content[:60]!r}"
    if _COMPANION_SURVEY.search("\n".join(plain[max(0, top - 6):top])):
        return "survey", "companion feedback survey is open; send 0 to dismiss"
    if _BUSY.search("\n".join(plain[bottom + 1:])):
        return "working", "companion footer shows esc to interrupt"
    return "idle", "companion input box is empty"


# --- verdict --------------------------------------------------------------

def agent_of(pane: dict) -> str:
    session = pane.get("coding_session") or {}
    process = pane.get("process") or {}
    return str(session.get("provider") or process.get("name") or "")


def assess_idle(pane: dict | None, text: str, *,
                allow_screen_only: bool = False,
                allow_busy: bool = False) -> dict:
    """Return ``{"ready", "reason", "state", "agent", "activity",
    "screen_state", "screen"}``.

    ``state`` is one of: idle, working, waiting, queued, occupied, survey,
    dead, no-composer, no-screen, unknown, unsupported, gone.
    ``screen_state`` is the screen-only classification, and ``screen`` is
    ``{"state", "detail"}`` for it. Both are None when no capture was assessed.

    ``allow_busy`` makes a pane with a turn in progress ready for a queued
    message, but only when the screen shows nothing pending (screen state
    working or idle). A draft, queued input, dialog, survey or dead session
    still blocks, so an FYI never types into someone's unsent text.
    ``allow_screen_only`` lets a pane with no turn boundary in its session log
    yet (a fresh session) be ready on screen evidence alone.
    """
    def verdict(ready: bool, state: str, reason: str, **extra) -> dict:
        return {"ready": ready, "reason": reason, "state": state, **extra}

    if not pane:
        return verdict(False, "gone", "pane is not in the Pane Center snapshot",
                       agent="", activity="", screen_state=None, screen=None)
    agent = agent_of(pane)
    activity = str(pane.get("activity") or "unknown")
    who = {"agent": agent, "activity": activity,
           "screen_state": None, "screen": None}
    if agent not in SUPPORTED:
        return verdict(False, "unsupported",
                       f"pane runs {agent or 'nothing'}, not {CODEX_ID} or {COMPANION_ID}",
                       **who)

    rows = _rows(text or "")
    ansi = "\x1b[" in (text or "")
    if not rows:
        state, detail = "no-screen", "screen capture is empty; did get-text fail?"
    elif agent == CODEX_ID:
        state, detail = _codex_screen(rows, ansi)
    else:
        state, detail = _companion_screen(rows, ansi)
    who["screen_state"] = state
    who["screen"] = {"state": state, "detail": detail}

    if state == "dead":
        return verdict(False, state, detail, **who)
    if activity == "waiting":
        return verdict(False, "waiting",
                       "agent is waiting on a choice or permission", **who)
    busy = activity == "working" or state == "working"
    if busy and allow_busy and state in ("working", "idle"):
        return verdict(True, "working",
                       "turn in progress with nothing pending on screen; "
                       "queued delivery allowed", **who)
    if activity == "working":
        note = "; the screen looks idle (interrupted turn?)" if state == "idle" else ""
        if allow_busy:  # a clean busy screen already returned ready above
            note = f"; allow_busy refused because the screen is {state}: {detail}"
        return verdict(False, "working",
                       f"session log shows a turn in progress{note}", **who)
    if state != "idle":
        return verdict(False, state, detail, **who)
    if activity == "idle":
        return verdict(True, "idle", f"turn complete and {detail}", **who)
    if allow_screen_only:
        return verdict(True, "idle",
                       f"activity={activity}; trusting screen: {detail}", **who)
    return verdict(False, "unknown",
                   f"activity={activity} has no turn boundary yet, although "
                   f"the screen looks idle ({detail})", **who)


# --- live capture ---------------------------------------------------------

def _run(cmd: list[str], timeout: float) -> str | None:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def snapshot(pane_id: int, *, ansi: bool = True,
             timeout: float = 30) -> tuple[dict | None, str]:
    """Fetch the Pane Center record and a screen capture for one pane."""
    lister = (["kilix-panes"] if shutil.which("kilix-panes")
              else ["kilix", "panes"])
    listing = _run(lister + ["list", "--json"], timeout)  # chatter is stderr
    pane = None
    if listing:
        for record in json.loads(listing).get("panes", []):
            if record.get("pane_id") == int(pane_id):
                pane = record
    cmd = ["kilix", "@", "get-text", "--match", f"id:{int(pane_id)}",
           "--extent", "screen"] + (["--ansi"] if ansi else [])
    return pane, _run(cmd, timeout) or ""


def probe(pane_id: int, **kwargs) -> dict:
    ansi = kwargs.pop("ansi", True)
    return {"pane_id": int(pane_id),
            **assess_idle(*snapshot(pane_id, ansi=ansi), **kwargs)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Report whether kilix panes are ready for a prompt "
                    "(debug entry point; the claudex CLI is the real interface).")
    ap.add_argument("panes", nargs="+", type=int)
    ap.add_argument("--plain", action="store_true",
                    help="capture without ANSI styling")
    ap.add_argument("--allow-screen-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    results = [probe(p, ansi=not args.plain,
                     allow_screen_only=args.allow_screen_only)
               for p in args.panes]
    if args.json:
        print(json.dumps(results, indent=1))
    else:
        for r in results:
            print(f"{r['pane_id']:<6}{'READY' if r['ready'] else 'no':<7}"
                  f"{r['state']:<12}{r['agent']:<8}{r['reason']}")
    return 0 if all(r["ready"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
