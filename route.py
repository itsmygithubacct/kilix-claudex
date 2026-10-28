#!/usr/bin/env python3
"""Named routes that open Codex, Claude, or Grok sessions.

Percentages are remaining usage. A provider whose ``checked_at`` is less than
30 minutes old is not read again. Qwen is never a max choice. A missing,
signed-out, or failed reading is not treated as zero.

``max`` opens one session, in this order:

1. Both weekly readings are known and each is under 20, and Grok still has
   weekly remaining: Grok's latest model at max, or xhigh when that model
   has no max.
2. One weekly reading leads the other by 50 points or more: that agent
   (gpt-6-astra max, or opus 5.5 max).
3. Claude's 5-hour reading is over 50: opus 5.5 max.
4. Otherwise gpt-6-astra max.

``worker`` opens both sides of a tier and does not read usage.
``security`` opens one session: Grok, else opus 5.5 high, else gpt-6-astra high.
Use ``kilix grammar``, ``kilix needle``, and ``kilix shell`` for tools in
the caller's pane.

A request may follow the route::

    claudex route max "fix src/app.py" --dry-run

``--dry-run`` prints the model and whether the request parses, and opens
nothing. A request parses when it names at least one file path and every path
is an existing file. A path starts with ``/``, ``~/``, ``./``, or ``../``, or
contains a slash. Quote a path that has spaces. A request that does not parse
does not open a pane. Codex, Claude, and Grok receive a parsed request as
their initial prompt.

Out of tokens means the five-hour or weekly window is fully used and has not
reset. Add another algorithm by extending ``ROUTE_NAMES`` and ``run``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import grok_status
from providers import CODEX_ID, COMPANION_COMMAND, COMPANION_ID, COMPANION_SKIP_PERMISSIONS, GROK_ID
import usage_pages

USAGE_TTL = 30 * 60
ROUTE_NAMES = ("max", "worker", "security")
WORKER_TIERS = ("low", "mid", "high", "max")
ACCOUNT_WINDOWS = ("five_hour", "weekly")
_LABEL = {"five_hour": "5h", "weekly": "weekly"}

# Model ids from the local Codex and Claude catalogs.
ASTRA = "gpt-6-astra"
SOL_61 = "gpt-6.1-sol"
TERRA_61 = "gpt-6.1-terra"
LUNA_6 = "gpt-6-luna"
OPUS_55 = "claude-opus-5-5"
SONNET_55 = "claude-sonnet-5-5"
GROK_FALLBACK = "grok-4.7"
GROK_EFFORT_FALLBACK = "xhigh"

# (agent, model, effort). Worker mid's Claude effort is the CLI word "medium".
WORKERS = {
    "max": ((CODEX_ID, SOL_61, "max"), (COMPANION_ID, SONNET_55, "max")),
    "high": ((CODEX_ID, SOL_61, "high"), (COMPANION_ID, SONNET_55, "high")),
    "mid": ((CODEX_ID, TERRA_61, "high"), (COMPANION_ID, SONNET_55, "medium")),
    "low": ((CODEX_ID, LUNA_6, "max"), (COMPANION_ID, SONNET_55, "low")),
}


class RouteError(Exception):
    pass


def _compact(value):
    number = round(float(value), 1)
    return int(number) if number == int(number) else number


def _provider(usage: dict, name: str) -> dict | None:
    providers = usage.get("providers") if isinstance(usage.get("providers"), dict) else {}
    entry = providers.get(name)
    return entry if isinstance(entry, dict) else None


def account_remaining(entry, name: str):
    """Remaining percent for the account window, or None when it was not read."""
    if not isinstance(entry, dict) or entry.get("status") != "ok":
        return None
    for item in entry.get("windows") or []:
        if not isinstance(item, dict) or item.get("name") != name:
            continue
        remaining = item.get("remaining_percent")
        if remaining is None and isinstance(item.get("used_percent"), (int, float)):
            remaining = round(max(0.0, 100 - item["used_percent"]), 1)
        if isinstance(remaining, (int, float)):
            return float(remaining)
    return None


def account_out(entry, now: float) -> bool:
    """True when a five-hour or weekly window is fully used and has not reset."""
    if not isinstance(entry, dict) or entry.get("status") != "ok":
        return False
    for item in entry.get("windows") or []:
        if not isinstance(item, dict) or item.get("name") not in ACCOUNT_WINDOWS:
            continue
        used = item.get("used_percent")
        if not isinstance(used, (int, float)) or used < 100:
            continue
        resets = item.get("resets_at")
        if isinstance(resets, (int, float)) and resets <= now:
            continue
        return True
    return False


def usable(entry, now: float) -> bool:
    return isinstance(entry, dict) and entry.get("status") == "ok" and not account_out(entry, now)


def fresh(entry, now: float) -> bool:
    checked = entry.get("checked_at") if isinstance(entry, dict) else None
    return isinstance(checked, (int, float)) and now - checked < USAGE_TTL


def load_usage(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def default_readers() -> dict:
    import claude_status
    import codex_status

    return {CODEX_ID: codex_status.read, COMPANION_ID: claude_status.read, GROK_ID: grok_status.read}


def ensure_usage(names, cache: Path, *, now: float, readers) -> tuple[dict, list]:
    """Reuse providers younger than 30 minutes. Read only the stale names."""
    usage = load_usage(cache)
    stale = [name for name in names if not fresh(_provider(usage, name), now)]
    if not stale:
        return usage, []
    report = {"read_at": now, "display": "direct", "providers": {}}
    for name in stale:
        try:
            entry = readers[name]()
        except KeyError as error:
            raise RouteError(f"no usage reader for {name}") from error
        if not isinstance(entry, dict):
            raise RouteError(f"usage reader for {name} did not return an object")
        if not isinstance(entry.get("checked_at"), (int, float)):
            entry = {**entry, "checked_at": now}
        report["providers"][name] = entry
    return usage_pages.merge_cache(cache, report), stale


def grok_target(home=None) -> tuple[str, str]:
    """Latest Grok model and max, or xhigh when the model menu has no max."""
    model, efforts = GROK_FALLBACK, {GROK_EFFORT_FALLBACK, "high", "medium", "low"}
    path = grok_status.grok_home(home) / "models_cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    models = data.get("models") if isinstance(data, dict) else None
    chosen = None
    chosen_id = None
    if isinstance(models, dict):
        for key, value in models.items():
            if not isinstance(value, dict):
                continue
            info = value.get("info") if isinstance(value.get("info"), dict) else value
            if not isinstance(info, dict) or info.get("hidden") is True:
                continue
            if chosen is None:
                chosen, chosen_id = info, key
            if "latest" in str(info.get("description") or "").lower():
                chosen, chosen_id = info, key
                break
    if chosen:
        model = str(chosen.get("model") or chosen.get("id") or chosen_id)
        listed = [str(item["id"]) for item in chosen.get("reasoning_efforts") or []
                  if isinstance(item, dict) and item.get("id")]
        if listed:
            efforts = set(listed)
    if "max" in efforts:
        return model, "max"
    if "xhigh" in efforts:
        return model, "xhigh"
    return model, sorted(efforts)[0]


def _session(agent: str, model: str | None, effort: str | None, *, title: str) -> dict:
    if agent == CODEX_ID:
        argv = ["codex", "--yolo", "-m", model, "-c", f"model_reasoning_effort={effort}"]
    elif agent == COMPANION_ID:
        argv = [COMPANION_COMMAND, COMPANION_SKIP_PERMISSIONS, "--model", model, "--effort", effort]
    elif agent == GROK_ID and model:
        argv = ["grok", "--yolo", "-m", model, "--effort", effort]
    elif agent == GROK_ID:
        argv = ["grok", "--yolo"]
    else:
        raise RouteError(f"no launch for {agent}")
    return {"agent": agent, "model": model, "effort": effort, "argv": argv, "title": title}


def _numbers(usage: dict, names=None) -> dict:
    found = {}
    for name in ((CODEX_ID, COMPANION_ID, GROK_ID) if names is None else names):
        entry = _provider(usage, name)
        if entry is None:
            continue
        if entry.get("status") != "ok":
            found[name] = entry.get("status") or "error"
            continue
        windows = {}
        for key, label in _LABEL.items():
            value = account_remaining(entry, key)
            if value is not None:
                windows[label] = _compact(value)
        found[name] = windows or "none"
    return found


def _why_not(entry) -> str:
    if not isinstance(entry, dict):
        return "unread"
    return str(entry.get("status") or "error")


def choose_max(usage: dict, *, now: float, grok_model) -> dict:
    codex = account_remaining(_provider(usage, CODEX_ID), "weekly")
    claude = account_remaining(_provider(usage, COMPANION_ID), "weekly")
    five_hour = account_remaining(_provider(usage, COMPANION_ID), "five_hour")
    skipped = []
    if codex is not None and claude is not None and codex < 20 and claude < 20:
        grok = _provider(usage, GROK_ID)
        weekly = account_remaining(grok, "weekly")
        if usable(grok, now) and weekly is not None and weekly > 0:
            model, effort = grok_model()
            return {"reason": "both weekly under 20", "skipped": skipped,
                    "sessions": [_session(GROK_ID, model, effort, title="claudex-route-max")]}
        if account_out(grok, now):
            skipped.append("grok out")
        elif weekly is not None and weekly <= 0:
            skipped.append("grok weekly empty")
        else:
            skipped.append(f"grok {_why_not(grok)}")
    if codex is not None and claude is not None and abs(codex - claude) >= 50:
        if codex > claude:
            session = _session(CODEX_ID, ASTRA, "max", title="claudex-route-max")
        else:
            session = _session(COMPANION_ID, OPUS_55, "max", title="claudex-route-max")
        return {"reason": f"weekly lead {_compact(abs(codex - claude))}", "skipped": skipped,
                "sessions": [session]}
    if five_hour is not None and five_hour > 50:
        return {"reason": "claude 5h over 50", "skipped": skipped,
                "sessions": [_session(COMPANION_ID, OPUS_55, "max", title="claudex-route-max")]}
    return {"reason": "default astra", "skipped": skipped,
            "sessions": [_session(CODEX_ID, ASTRA, "max", title="claudex-route-max")]}


def choose_security(usage: dict, *, now: float) -> dict:
    grok = _provider(usage, GROK_ID)
    if usable(grok, now):
        return {"reason": "grok has tokens", "skipped": [],
                "sessions": [_session(GROK_ID, None, None, title="claudex-route-security")]}
    skipped = [f"grok {('out' if account_out(grok, now) else _why_not(grok))}"]
    claude = _provider(usage, COMPANION_ID)
    if usable(claude, now):
        return {"reason": "claude has tokens", "skipped": skipped,
                "sessions": [_session(COMPANION_ID, OPUS_55, "high", title="claudex-route-security")]}
    skipped.append(f"claude {('out' if account_out(claude, now) else _why_not(claude))}")
    return {"reason": "codex fallback", "skipped": skipped,
            "sessions": [_session(CODEX_ID, ASTRA, "high", title="claudex-route-security")]}


def choose_worker(tier: str) -> dict:
    if tier not in WORKERS:
        raise RouteError(f"claudex route worker needs {', '.join(WORKER_TIERS)}")
    sessions = [_session(agent, model, effort, title=f"claudex-route-worker-{tier}-{agent}")
                for agent, model, effort in WORKERS[tier]]
    return {"reason": "worker pair", "skipped": [], "sessions": sessions}


def _ages(usage: dict, names, now: float) -> dict:
    ages = {}
    for name in names:
        entry = _provider(usage, name)
        checked = entry.get("checked_at") if isinstance(entry, dict) else None
        if isinstance(checked, (int, float)):
            ages[name] = max(0, int(now - checked))
    return ages


_PATH_TOKEN = re.compile(r"""(?P<q>['"])(?P<quoted>.*?)(?P=q)|(?P<bare>\S+)""")


def split_request(name: str, words) -> tuple[str | None, str | None]:
    """Separate a worker tier from the request text."""
    words = [word for word in (words or []) if word is not None]
    if name == "worker":
        if not words or words[0] not in WORKER_TIERS:
            raise RouteError(f"claudex route worker needs {', '.join(WORKER_TIERS)}")
        tier, words = words[0], words[1:]
    elif words and words[0] in WORKER_TIERS:
        raise RouteError(f"claudex route {name} does not take a tier")
    else:
        tier = None
    if not words:
        return tier, None
    return tier, words[0] if len(words) == 1 else " ".join(words)


def _path_token(token: str) -> str | None:
    text = token.strip().rstrip(".,;:)]")
    if not text or text.startswith(("http://", "https://")):
        return None
    if text.startswith(("/", "~/", "./", "../")) or "/" in text:
        return text
    return None


def extract_paths(prompt: str) -> list[str]:
    found = []
    seen = set()
    for match in _PATH_TOKEN.finditer(prompt or ""):
        raw = match.group("quoted") if match.group("quoted") is not None else match.group("bare")
        path = _path_token(raw or "")
        if path and path not in seen:
            seen.add(path)
            found.append(path)
    return found


def check_request(prompt: str, cwd: Path) -> dict:
    """A request parses when every path it names is an existing file."""
    paths = extract_paths(prompt.strip()) if prompt and prompt.strip() else []
    if not paths:
        return {"ok": False, "reason": "no file path", "files": []}
    files = []
    problems = []
    root = Path(cwd)
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = root / path
        try:
            path = path.resolve()
        except OSError:
            problems.append(f"missing {raw}")
            continue
        shown = str(path)
        if path.is_file():
            files.append(shown)
        elif path.exists():
            problems.append(f"not a file {shown}")
        else:
            problems.append(f"missing {shown}")
    if problems:
        return {"ok": False, "reason": "; ".join(problems), "files": files}
    return {"ok": True, "reason": "ok", "files": files}


def _with_prompt(sessions, prompt: str) -> list:
    attached = []
    for session in sessions:
        argv = list(session["argv"])
        if session["agent"] in (CODEX_ID, COMPANION_ID, GROK_ID):
            argv.append(prompt)
        attached.append({**session, "argv": argv})
    return attached


def kilix_launch(*args: str) -> str:
    try:
        done = subprocess.run(["kilix", "@", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RouteError(f"kilix @ {args[0]} failed: {error}") from error
    if done.returncode:
        raise RouteError(done.stderr.strip() or f"kilix @ {args[0]} exited {done.returncode}")
    return done.stdout.strip()


def open_sessions(sessions, *, cwd: Path, launch) -> tuple[list, list]:
    """Open the first session as a tab and each later session beside it."""
    tab = "claudex-route"
    ids: list = []
    commands = []
    for index, session in enumerate(sessions):
        if index == 0:
            where = ["--type=tab", f"--tab-title={tab}"]
        else:
            where = ["--match", f"window_id:{ids[0]}", "--type=window", "--location=vsplit",
                     "--next-to", f"id:{ids[0]}"]
        args = ["launch", *where, f"--title={session['title']}", f"--cwd={cwd}", "--hold",
                "--keep-focus", "--env", f"PATH={os.environ.get('PATH', '')}", *session["argv"]]
        commands.append(args)
        if launch is None:
            ids.append(None)
            continue
        output = launch(*args)
        try:
            ids.append(int(str(output).split()[-1]))
        except (ValueError, IndexError) as error:
            raise RouteError(f"kilix returned no window id for {session['title']}: {output!r}") from error
    return commands, ids


def run(name: str, tier: str | None = None, *, cache: Path, cwd: Path, now: float | None = None,
        readers=None, launch=None, dry_run: bool = False, grok_home=None,
        prompt: str | None = None) -> dict:
    """Choose a route and open it. ``launch`` receives the kilix arguments."""
    import time

    if name not in ROUTE_NAMES:
        raise RouteError(f"unknown route {name!r}; choose from {', '.join(ROUTE_NAMES)}")
    if name == "worker" and tier not in WORKER_TIERS:
        raise RouteError(f"claudex route worker needs {', '.join(WORKER_TIERS)}")
    if name != "worker" and tier is not None:
        raise RouteError(f"claudex route {name} does not take a tier")
    now = time.time() if now is None else now
    readers = default_readers() if readers is None else readers
    usage: dict = {}
    refreshed: list = []
    consulted: list = []
    if name == "worker":
        decision = choose_worker(tier)
    elif name == "max":
        consulted = [CODEX_ID, COMPANION_ID]
        usage, refreshed = ensure_usage(consulted, cache, now=now, readers=readers)
        if _both_under(usage):
            usage, more = ensure_usage([GROK_ID], cache, now=now, readers=readers)
            refreshed = [*refreshed, *more]
            consulted.append(GROK_ID)
        decision = choose_max(usage, now=now, grok_model=lambda: grok_target(grok_home))
    else:
        consulted = [GROK_ID]
        usage, refreshed = ensure_usage(consulted, cache, now=now, readers=readers)
        if not usable(_provider(usage, GROK_ID), now):
            usage, more = ensure_usage([COMPANION_ID], cache, now=now, readers=readers)
            refreshed = [*refreshed, *more]
            consulted.append(COMPANION_ID)
        decision = choose_security(usage, now=now)
    request = check_request(prompt, cwd) if prompt is not None else None
    if request is not None and request["ok"]:
        decision["sessions"] = _with_prompt(decision["sessions"], prompt)
    held = request is not None and not request["ok"]
    commands, ids = ([], [])
    if not dry_run and not held:
        commands, ids = open_sessions(decision["sessions"], cwd=cwd, launch=launch or kilix_launch)
    return {**decision, "route": name, "tier": tier, "dry_run": dry_run, "refreshed": refreshed,
            "usage": _numbers(usage, consulted), "cache_age_s": _ages(usage, consulted, now),
            "window_ids": ids, "commands": commands, "request": request}


def _both_under(usage: dict) -> bool:
    codex = account_remaining(_provider(usage, CODEX_ID), "weekly")
    claude = account_remaining(_provider(usage, COMPANION_ID), "weekly")
    return codex is not None and claude is not None and codex < 20 and claude < 20


def format_line(decision: dict) -> str:
    request = decision.get("request")
    blocked = isinstance(request, dict) and not request.get("ok")
    verb = "would open" if decision.get("dry_run") or blocked else "opened"
    head = decision["route"] if not decision.get("tier") else f"{decision['route']} {decision['tier']}"
    parts = []
    for session in decision["sessions"]:
        bits = [session["agent"]]
        if session.get("model"):
            bits.append(session["model"])
        if session.get("effort"):
            bits.append(session["effort"])
        parts.append(" ".join(bits))
    line = f"{verb} {head} " + "; ".join(parts)
    if not isinstance(request, dict):
        return line
    if request.get("ok"):
        return f"{line}\nparse ok {' '.join(request.get('files') or [])}"
    return f"{line}\nparse fail {request.get('reason') or 'no file path'}"


def public_record(decision: dict) -> dict:
    """The decision a model can read. No tmux screen and no billing body."""
    sessions = [{"agent": s["agent"], "model": s["model"], "effort": s["effort"],
                 "argv": s["argv"], "title": s["title"]} for s in decision["sessions"]]
    record = {"route": decision["route"], "tier": decision.get("tier"), "reason": decision["reason"],
              "skipped": decision.get("skipped") or [], "dry_run": bool(decision.get("dry_run")),
              "refreshed": decision.get("refreshed") or [], "cache_age_s": decision.get("cache_age_s") or {},
              "usage": decision.get("usage") or {}, "sessions": sessions,
              "window_ids": decision.get("window_ids") or []}
    request = decision.get("request")
    if isinstance(request, dict):
        record["request"] = {"ok": bool(request.get("ok")), "reason": request.get("reason") or "",
                              "files": list(request.get("files") or [])}
    return record
