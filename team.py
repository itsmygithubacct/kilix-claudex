"""Plan and launch 1–12 individually configured sessions in one Kilix grid."""
import json
from pathlib import Path
import re
import shlex
import subprocess

import route
from providers import CODEX_ID, COMPANION_ID, GROK_ID

PROVIDERS = ("auto", CODEX_ID, COMPANION_ID, GROK_ID)
SESSION_FIELDS = {"title", "provider", "tier", "model", "effort", "cwd", "document", "prompt", "prompt_addon"}
DEFAULT_FIELDS = SESSION_FIELDS - {"title", "document", "prompt_addon"}


class TeamError(ValueError):
    pass


def _text(value, label, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or "\0" in value:
        raise TeamError(f"{label} must be {'a string' if empty else 'a nonempty string'} without NUL")
    return value


def _path(value, base, label, *, directory=False):
    text = _text(value, label)
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise TeamError(f"{label} contains control characters")
    path = Path(text).expanduser()
    path = (path if path.is_absolute() else base / path).resolve()
    if not (path.is_dir() if directory else path.is_file()):
        raise TeamError(f"{label} is not an existing {'directory' if directory else 'file'}: {path}")
    return path


def read_spec(path):
    path = Path(path).expanduser().resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TeamError(f"cannot read team plan: {error}") from error
    if not isinstance(value, dict) or set(value) - {"name", "defaults", "sessions"}:
        raise TeamError("team plan must be an object with name, defaults and sessions")
    name = value.get("name", "claudex-team")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", name):
        raise TeamError("team name must be 1–64 letters, digits, dots, underscores or hyphens")
    defaults = value.get("defaults", {})
    if not isinstance(defaults, dict) or set(defaults) - DEFAULT_FIELDS:
        raise TeamError("invalid team defaults; use provider, tier, model, effort, cwd or prompt")
    rows = value.get("sessions")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 12:
        raise TeamError("a team must contain 1–12 sessions")
    titles, documents, sessions = set(), set(), []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) - SESSION_FIELDS:
            raise TeamError(f"session {index} has unknown fields or is not an object")
        item = {**defaults, **row}
        title = _text(item.get("title", f"{name}-{index}"), f"session {index} title")
        if any(ord(char) < 32 or ord(char) == 127 for char in title) or title in titles:
            raise TeamError("session titles must be unique and contain no control characters")
        titles.add(title)
        cwd = _path(item.get("cwd", "."), path.parent, f"{title} cwd", directory=True)
        provider = item.get("provider", "auto")
        tier = item.get("tier", "low")
        if provider not in PROVIDERS or tier not in route.WORKER_TIERS:
            raise TeamError(f"{title}: invalid provider or worker tier")
        if item.get("model") is not None and provider == "auto":
            raise TeamError(f"{title}: choose a provider explicitly when setting a model")
        for field in ("model", "effort"):
            if field in item:
                token = _text(item[field], f"{title} {field}")
                if token.startswith("-") or any(char.isspace() or ord(char) < 32 or ord(char) == 127
                                                for char in token):
                    raise TeamError(f"{title}: {field} must be one identifier")
        prompt = _text(item.get("prompt", ""), f"{title} prompt", empty=True)
        addon = _text(item.get("prompt_addon", ""), f"{title} prompt_addon", empty=True)
        document = None
        if "document" in item:
            document = _path(item["document"], cwd, f"{title} document")
            if document in documents:
                raise TeamError(f"each assigned document must be unique: {document}")
            documents.add(document)
        pieces = [prompt] if prompt else []
        if document:
            pieces.append(f"Read your assigned document before acting: {shlex.quote(str(document))}")
        if addon:
            pieces.append(addon)
        sessions.append({**item, "title": title, "cwd": str(cwd), "provider": provider, "tier": tier,
                         "document": str(document) if document else None, "prompt": prompt,
                         "prompt_addon": addon, "initial_prompt": "\n\n".join(pieces)})
    return {"name": name, "source": str(path), "sessions": sessions}


def make_plan(path, *, cache, worker_provider="auto", resolver=None):
    # Validate every path and field before reading an account or opening a pane.
    spec = read_spec(path)
    resolver = resolver or route.run
    decisions, sessions = {}, []
    for row in spec["sessions"]:
        provider = row["provider"]
        if provider == "auto":
            key = row["tier"]
            if key not in decisions:
                decisions[key] = resolver("worker", key, cache=cache, cwd=Path(row["cwd"]),
                                          dry_run=True, provider=worker_provider)
            choice = decisions[key]["sessions"][0]
            provider, model, effort = choice["agent"], choice["model"], choice["effort"]
        elif provider == GROK_ID:
            model, effort = route.grok_target()
        else:
            choice = route.choose_worker(row["tier"], provider)["sessions"][0]
            model, effort = choice["model"], choice["effort"]
        model, effort = row.get("model", model), row.get("effort", effort)
        session = route._session(provider, model, effort, title=row["title"])
        if row["initial_prompt"]:
            session = route._with_prompt([session], row["initial_prompt"])[0]
        sessions.append({**session, "cwd": row["cwd"], "document": row["document"],
                         "prompt": row["prompt"], "prompt_addon": row["prompt_addon"],
                         "initial_prompt": row["initial_prompt"]})
    return {"status": "planned", "name": spec["name"], "source": spec["source"], "sessions": sessions}


def existing_titles():
    try:
        done = subprocess.run(["kilix", "@", "ls"], capture_output=True, text=True, timeout=30)
        if done.returncode:
            raise TeamError("cannot list existing Kilix panes")
        windows = json.loads(done.stdout)
        if not isinstance(windows, list):
            raise ValueError("expected a window list")
        return {pane["title"] for window in windows for tab in window.get("tabs", [])
                for pane in tab.get("windows", []) if pane.get("title")}
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired) as error:
        raise TeamError(f"cannot inspect existing Kilix titles: {error}") from error


def launch(plan, *, launcher=None, titles=None):
    clashes = {session["title"] for session in plan["sessions"]} & (
        existing_titles() if titles is None else set(titles))
    if clashes:
        raise TeamError("session titles already exist: " + ", ".join(sorted(clashes)))
    commands, ids = route.open_sessions(plan["sessions"], cwd=Path(plan["sessions"][0]["cwd"]),
                                       name=plan["name"], launch=launcher or route.kilix_launch)
    return {**plan, "status": "launched", "window_ids": ids, "commands": commands}
