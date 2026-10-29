#!/usr/bin/env python3
"""Launch and check the five-pane claudex team tab in kilix.

The tab is a kilix session grid (``kilix session grid add``): two rows, filled
column by column in launch order, so the team reads left to right as

    | orchestrator | worker_1 | worker_3 |
    |--------------|----------|          |
    | companion    | worker_2 |          |

    claudex-orchestrator  codex --yolo on gpt-6-astra at max reasoning effort
    claudex-companion     the companion CLI on its flagship model, permissions skipped
    claudex_worker_1      grok --yolo while grok has tokens, else a ~/qwen-test model via omp
    claudex_worker_2      a ~/qwen-test model via the companion CLI
    claudex_worker_3      codex --yolo on gpt-5.3-codex-spark while Spark has tokens, else qwen via omp

Token checks take the newest of three sources: the ``claudex usage`` reading
(refreshed first when stale; Codex comes from the ``codex_usage`` tmux
``/status`` screen, and Grok comes from the grok CLI billing request), grok's
billing log, and Codex rollouts for the Spark limit bucket.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

import browser  # noqa: E402
import limits  # noqa: E402
from pane_loop import default_config  # noqa: E402
from providers import (CODEX_ID, COMPANION_COMMAND, COMPANION_SKIP_PERMISSIONS,  # noqa: E402
                       COMPANION_TEAM_MODEL, GROK_ID, QWEN_COMPANION_LAUNCHER, QWEN_OMP_LAUNCHER)
import codex_status  # noqa: E402
import grok_status  # noqa: E402
import usage_pages  # noqa: E402

TAB_TITLE = "claudex-team"
QWEN_MODEL_ALIAS = "max"
SPARK_MODEL = "gpt-5.3-codex-spark"
SPARK_LIMIT_ID = "codex_bengalfox"  # the Spark bucket in Codex rollouts
USAGE_MAX_AGE = 15 * 60
GRID_LAYOUT = "sessions"
# Launch order, which is also the grid's pane order: the first pane opens the tab.
ORDER = ("orchestrator", "companion", "worker_1", "worker_2", "worker_3")
TOKEN_PROVIDERS = {"grok": GROK_ID, "spark": CODEX_ID}


class TeamError(Exception):
    pass


# --- the team ---------------------------------------------------------------

def qwen_dir() -> Path:
    return Path(os.environ.get("CLAUDEX_QWEN_DIR") or "~/qwen-test").expanduser()


def qwen_launcher(name: str, *extra: str) -> list[str]:
    return [str(qwen_dir() / name), "--here", QWEN_MODEL_ALIAS, "--", *extra]


def team() -> list[dict]:
    omp = {"name": f"qwen {QWEN_MODEL_ALIAS} via omp", "argv": qwen_launcher(QWEN_OMP_LAUNCHER, "--approval-mode=yolo")}
    return [
        {"role": "orchestrator", "title": "claudex-orchestrator", "choices": [
            {"name": "codex gpt-6-astra max",
             "argv": ["codex", "--yolo", "-m", "gpt-6-astra", "-c", "model_reasoning_effort=max"]}]},
        {"role": "companion", "title": "claudex-companion", "choices": [
            {"name": COMPANION_TEAM_MODEL,
             "argv": [COMPANION_COMMAND, COMPANION_SKIP_PERMISSIONS, "--model", COMPANION_TEAM_MODEL]}]},
        {"role": "worker_1", "title": "claudex_worker_1", "choices": [
            {"name": "grok", "argv": ["grok", "--yolo"], "tokens": "grok"}, omp]},
        {"role": "worker_2", "title": "claudex_worker_2", "choices": [
            {"name": f"qwen {QWEN_MODEL_ALIAS} via {COMPANION_COMMAND}",
             "argv": qwen_launcher(QWEN_COMPANION_LAUNCHER, COMPANION_SKIP_PERMISSIONS)}]},
        {"role": "worker_3", "title": "claudex_worker_3", "choices": [
            {"name": f"codex {SPARK_MODEL}", "argv": ["codex", "--yolo", "-m", SPARK_MODEL], "tokens": "spark"},
            dict(omp)]},
    ]


# --- tokens -----------------------------------------------------------------

def app_dir() -> Path:
    return default_config().parent


def usage_cache() -> Path:
    return app_dir() / ".loop" / "usage-pages.json"


def load_usage(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _live(windows, now: float) -> list[dict]:
    return [w for w in windows or [] if isinstance(w, dict) and isinstance(w.get("used_percent"), (int, float))
            and not (w.get("resets_at") and w["resets_at"] <= now)]


def _when(epoch) -> str:
    return time.strftime("%a %H:%M", time.localtime(epoch)) if epoch else "at an unknown time"


def token_check(kind: str, *, now: float, usage: dict, codex_home=None, grok_home=None) -> dict:
    """``{"state": available|exhausted|unknown, "reason", "source"}`` from the newest current evidence."""
    cached = usage.get("providers") if isinstance(usage.get("providers"), dict) else {}
    observations: list[tuple[float, str, list]] = []
    if kind == "grok":
        entry = cached.get(GROK_ID) or {}
        if entry.get("status") == "ok":
            observations.append((entry.get("checked_at") or 0.0, "usage page", entry.get("windows") or []))
        evidence = limits.grok_evidence(now=now, home=grok_home)
        if evidence:
            observations.append((evidence["observed_at"], "grok log",
                                 [{"label": name, **w} for name, w in evidence["windows"].items()]))
    elif kind == "spark":
        entry = cached.get(CODEX_ID) or {}
        spark = [w for w in entry.get("windows") or [] if "spark" in str(w.get("label") or w.get("name")).lower()]
        if entry.get("status") == "ok" and spark:
            observations.append((entry.get("checked_at") or 0.0, "usage page", spark))
        newest = None
        for path in limits.recent_codex_rollouts(codex_home, count=12):
            for limit_id, observed, snapshot in limits.codex_snapshots(path):
                if limit_id == SPARK_LIMIT_ID and (newest is None or (observed or 0) > (newest[0] or 0)):
                    newest = (observed, snapshot)
        if newest:
            windows = limits._codex_windows(newest[1], now)
            observations.append((newest[0] or 0.0, "codex rollout",
                                 [{"label": name, **w} for name, w in windows.items()]))
    else:
        raise TeamError(f"unknown token check {kind!r}")
    current = [o for o in observations if _live(o[2], now)]
    if not current:
        reason = "every observed window has reset since" if observations else "no usage evidence"
        return {"state": "unknown", "reason": reason, "source": None}
    observed, source, windows = max(current, key=lambda o: o[0])
    live = _live(windows, now)
    full = [w for w in live if w["used_percent"] >= 100]
    if full:
        label = str(full[0].get("label") or full[0].get("name")).replace("five_hour", "5 hour").replace("_", " ")
        return {"state": "exhausted", "source": source, "observed_at": observed,
                "reason": f"{label} {full[0]['used_percent']:g}% used, resets {_when(full[0].get('resets_at'))}"}
    busiest = max(live, key=lambda w: w["used_percent"])
    return {"state": "available", "source": source, "observed_at": observed,
            "reason": f"at most {busiest['used_percent']:g}% used"}


def refresh_usage(kinds, *, force: bool = False, now: float | None = None) -> tuple[dict, str | None]:
    """Refresh a stale Codex /status reading and any stale usage pages. Never fatal."""
    now = time.time() if now is None else now
    path = usage_cache()
    usage = load_usage(path)
    cached = usage.get("providers") if isinstance(usage.get("providers"), dict) else {}
    wanted = sorted({TOKEN_PROVIDERS[kind] for kind in kinds})
    stale = [p for p in wanted if force or now - ((cached.get(p) or {}).get("checked_at") or 0) > USAGE_MAX_AGE]
    if not stale:
        return usage, None
    notes = []
    report = {"read_at": now, "providers": {}}
    pages = [name for name in stale if name not in (CODEX_ID, GROK_ID)]
    if pages:
        try:
            report = usage_pages.read_usage(pages, app_dir() / "usage-browser", display="virtual", timeout=40)
        except (browser.BrowserError, OSError) as error:
            notes.append(f"usage pages not refreshed ({error})")
            report = {"read_at": now, "providers": {}}
    if CODEX_ID in stale:
        entry = codex_status.read()
        report.setdefault("providers", {})[CODEX_ID] = entry
        if entry.get("status") != "ok":
            notes.append(f"codex status not refreshed ({entry.get('reason')})")
    if GROK_ID in stale:
        entry = grok_status.read()
        report.setdefault("providers", {})[GROK_ID] = entry
        if entry.get("status") != "ok":
            notes.append(f"grok billing not refreshed ({entry.get('reason')})")
    if report["providers"]:
        usage = usage_pages.merge_cache(path, report)
    note = "; ".join(notes)
    return usage, f"{note}; using cached and local evidence" if note else None


def make_plan(*, now: float | None = None, usage: dict | None = None, codex_home=None, grok_home=None) -> list[dict]:
    now = time.time() if now is None else now
    usage = load_usage(usage_cache()) if usage is None else usage
    checks: dict[str, dict] = {}
    plan = []
    for member in team():
        chosen, notes = None, []
        for choice in member["choices"]:
            kind = choice.get("tokens")
            if kind:
                if kind not in checks:
                    checks[kind] = token_check(kind, now=now, usage=usage, codex_home=codex_home, grok_home=grok_home)
                notes.append({"choice": choice["name"], **checks[kind]})
                if checks[kind]["state"] == "exhausted":
                    continue
            chosen = choice
            break
        chosen = chosen or member["choices"][-1]
        missing = [arg for arg in chosen["argv"][:1] if "/" in arg and not Path(arg).is_file()]
        plan.append({"role": member["role"], "title": member["title"], "chosen": chosen,
                     "checks": notes, "missing": missing})
    return plan


# --- kilix ------------------------------------------------------------------

class Kilix:
    def __init__(self, executable: str = "kilix"):
        self.executable = executable

    def run(self, *args: str) -> str:
        try:
            done = subprocess.run([self.executable, "@", *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TeamError(f"kilix @ {args[0]} failed: {error}") from error
        if done.returncode:
            raise TeamError(done.stderr.strip() or f"kilix @ {args[0]} exited {done.returncode}")
        return done.stdout.strip()

    def session(self, *args: str) -> dict:
        """Run ``kilix session ARGS`` and return its JSON receipt."""
        try:
            done = subprocess.run([self.executable, "session", *args], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TeamError(f"kilix session {args[0]} failed: {error}") from error
        try:
            value = json.loads(done.stdout or "null")
        except ValueError:
            value = None
        if done.returncode or not isinstance(value, dict):
            reason = value.get("error") if isinstance(value, dict) else None
            raise TeamError(reason or done.stderr.strip() or f"kilix session {args[0]} exited {done.returncode}")
        return value

    def ls(self) -> list:
        try:
            value = json.loads(self.run("ls") or "[]")
        except ValueError as error:
            raise TeamError("kilix @ ls did not return JSON") from error
        if not isinstance(value, list):
            raise TeamError("kilix @ ls did not return a window list")
        return value

    def panes(self) -> dict:
        executable = os.environ.get("CLAUDEX_KILIX_PANES", "kilix-panes")
        try:
            done = subprocess.run([executable, "list", "--json"], capture_output=True, text=True, timeout=30)
            data = json.loads(done.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return {}
        return {p.get("pane_id"): p for p in data.get("panes") or [] if isinstance(p, dict)}


def windows_by_title(ls) -> dict[str, list[tuple[dict, dict]]]:
    found: dict[str, list[tuple[dict, dict]]] = {}
    for os_window in ls or []:
        for tab in os_window.get("tabs") or []:
            for window in tab.get("windows") or []:
                found.setdefault(window.get("title"), []).append((tab, window))
    return found


def grid_order(tab: dict, names: dict) -> tuple:
    """Role names in the tab's grid order: its window groups, else its windows."""
    state = tab.get("layout_state") or {}
    groups = [g.get("window_ids") or [] for g in (state.get("all_windows") or {}).get("window_groups") or []]
    titles = {w.get("id"): w.get("title") for w in tab.get("windows") or []}
    ids = [group[0] for group in groups if group] or list(titles)
    return tuple(names.get(titles.get(i), titles.get(i)) for i in ids)


def layout_matches(tab: dict, names: dict) -> bool:
    return tab.get("layout") == GRID_LAYOUT and grid_order(tab, names) == ORDER


def launch(kilix, plan: list[dict], *, cwd: Path, placeholder: bool = False, allow_duplicates: bool = False,
           dry_run: bool = False, background: bool = False) -> dict:
    members = {member["role"]: member for member in plan}
    missing = [f"{m['title']}: {path}" for m in plan for path in m["missing"]]
    if missing and not placeholder:
        raise TeamError("launcher not found: " + "; ".join(missing))
    titles = {role: f"test-{m['title']}" if placeholder else m["title"] for role, m in members.items()}
    existing = windows_by_title(kilix.ls())
    clashes = sorted(title for title in titles.values() if title in existing)
    if clashes and not allow_duplicates:
        raise TeamError(f"panes with these titles already exist: {', '.join(clashes)}. "
                        "claudex finds panes by title; see claudex_team.py status, or pass --allow-duplicates")
    tab_title = f"{TAB_TITLE}-test" if placeholder else TAB_TITLE
    ids: dict[str, object] = {}
    commands = []
    for role in ORDER:
        argv = members[role]["chosen"]["argv"]
        if placeholder:
            argv = ["bash", "-c", f"printf '%s\\n' {shlex.quote(titles[role] + ': ' + shlex.join(argv))}; exec bash"]
        # The orchestrator opens a new grid tab; the rest join the grid that holds it.
        where = ["--new"] if role == ORDER[0] else ["--into", str(ids[ORDER[0]])]
        args = ["grid", "add", "--name", tab_title, *where, "--title", titles[role], "--cwd", str(cwd),
                "--var", f"claudex_team={role}", "--env", f"PATH={os.environ.get('PATH', '')}", "--", *argv]
        commands.append({"role": role, "title": titles[role], "argv": argv, "kilix": ["kilix", "session", *args]})
        if dry_run:
            ids[role] = role
            continue
        try:
            receipt = kilix.session(*args)
            pane_id = receipt["pane"]["pane_id"]
            if type(pane_id) is not int or pane_id <= 0:
                raise ValueError("receipt has no positive integer pane id")
            ids[role] = pane_id
        except (TeamError, KeyError, TypeError, ValueError) as error:
            raise TeamError(f"launch failed for {titles[role]}: {error}; "
                            f"confirmed panes already opened: {ids}; inspect before retrying") from error
    if not dry_run and not background:
        kilix.run("focus-window", "--match", f"id:{ids['orchestrator']}")
    return {"status": "planned" if dry_run else "launched", "tab_title": tab_title,
            "window_ids": ids, "commands": commands}


def status(kilix, *, now: float | None = None, usage: dict | None = None, codex_home=None, grok_home=None,
           placeholder: bool = False) -> dict:
    now = time.time() if now is None else now
    by_title = windows_by_title(kilix.ls())
    panes = kilix.panes()
    plan = make_plan(now=now, usage=usage, codex_home=codex_home, grok_home=grok_home)
    prefix = "test-" if placeholder else ""
    names = {prefix + member["title"]: member["role"] for member in plan}
    members, tabs = [], {}
    for member in plan:
        row = {"role": member["role"], "title": prefix + member["title"], "planned": member["chosen"]["name"],
               "checks": member["checks"], "windows": []}
        for tab, window in by_title.get(prefix + member["title"]) or []:
            pane = panes.get(window.get("id")) or {}
            agent = limits.pane_agent(pane)[0] if pane else ""
            argv = (pane.get("process") or {}).get("argv") or []
            foreground = [" ".join(p.get("cmdline") or []) for p in window.get("foreground_processes") or []]
            row["windows"].append({"window_id": window.get("id"), "tab_id": tab.get("id"), "tab_title": tab.get("title"),
                                   "agent": agent or None, "activity": pane.get("activity"),
                                   "command": " ".join(argv) or (foreground[0] if foreground else None)})
            tabs[tab.get("id")] = tab
        members.append(row)
    layouts = []
    for tab_id, tab in tabs.items():
        layouts.append({"tab_id": tab_id, "tab_title": tab.get("title"), "layout": tab.get("layout"),
                        "order": list(grid_order(tab, names)), "matches": layout_matches(tab, names)})
    ready = all(len(row["windows"]) == 1 for row in members) and len(layouts) == 1 and layouts[0]["matches"]
    return {"status": "ok" if ready else "incomplete", "members": members, "layouts": layouts}


# --- output -----------------------------------------------------------------

def _check_lines(checks) -> list[str]:
    return [f"    {c['choice']}: {c['state']}, {c['reason']}" + (f" ({c['source']})" if c.get("source") else "")
            for c in checks]


def print_plan(plan, note=None) -> None:
    for member in plan:
        print(f"{member['role']:<13} {member['title']:<21} {member['chosen']['name']}")
        for line in _check_lines(member["checks"]):
            print(line)
        for path in member["missing"]:
            print(f"    missing launcher: {path}")
    if note:
        print(note)


def print_status(value) -> None:
    print(f"{'ROLE':<13} {'TITLE':<21} {'PANE':<6} {'TAB':<24} {'AGENT':<7} {'ACTIVITY':<9} COMMAND")
    checks = {}
    for row in value["members"]:
        for check in row["checks"]:
            checks[check["choice"]] = check
        if not row["windows"]:
            print(f"{row['role']:<13} {row['title']:<21} {'-':<6} {'missing':<24} {'-':<7} {'-':<9} would run {row['planned']}")
        for window in row["windows"]:
            tab = f"{window['tab_id']} {window['tab_title'] or ''}"[:23]
            print(f"{row['role']:<13} {row['title']:<21} {window['window_id']!s:<6} {tab:<24} "
                  f"{window['agent'] or '-':<7} {window['activity'] or '-':<9} {(window['command'] or '-')[:60]}")
    for layout in value["layouts"]:
        verdict = ("matches the team layout" if layout["matches"] else
                   f"differs from the team layout: {layout['layout']} {', '.join(map(str, layout['order']))}")
        print(f"layout: tab {layout['tab_id']} {layout['tab_title']!r} {verdict}")
    for name, check in checks.items():
        print(f"tokens: {name}: {check['state']}, {check['reason']}" + (f" ({check['source']})" if check.get("source") else ""))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="claudex_team.py", description="Launch and check the claudex team tab in kilix.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (("plan", "show which agent each pane would run, with the token checks"),
                       ("launch", "open the team tab"),
                       ("status", "find the team panes and check their layout, commands and tokens")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--json", action="store_true", help="print JSON")
        if name == "status":
            sub.add_argument("--placeholder", action="store_true", help="check the test-* panes of a placeholder launch")
        if name != "status":
            freshness = sub.add_mutually_exclusive_group()
            freshness.add_argument("--refresh", action="store_true",
                                   help="read Codex /status and the grok billing request first")
            freshness.add_argument("--no-refresh", action="store_true", help="use cached and local usage evidence only")
    start = commands.choices["launch"]
    start.add_argument("--cwd", type=Path, default=Path.cwd(), help="working directory for every pane (default: here)")
    start.add_argument("--dry-run", action="store_true", help="print the kilix commands without running them")
    start.add_argument("--placeholder", action="store_true",
                       help="open the same layout with shells that print each command, titled test-*")
    start.add_argument("--allow-duplicates", action="store_true", help="launch even if team titles already exist")
    start.add_argument("--background", action="store_true", help="don't focus the new tab")
    args = parser.parse_args(argv)
    kilix = Kilix()
    try:
        if args.command == "status":
            value = status(kilix, placeholder=args.placeholder)
            print(json.dumps(value, indent=2, ensure_ascii=False)) if args.json else print_status(value)
            return 0 if value["status"] == "ok" else 2
        note = usage = None
        placeholder = getattr(args, "placeholder", False)
        if not args.no_refresh and (args.refresh or not placeholder):
            kinds = {choice["tokens"] for member in team() for choice in member["choices"] if choice.get("tokens")}
            usage, note = refresh_usage(kinds, force=args.refresh)
        plan = make_plan(usage=usage)
        if args.command == "plan":
            print(json.dumps({"plan": plan, "note": note}, indent=2, ensure_ascii=False)) if args.json else print_plan(plan, note)
            return 1 if any(member["missing"] for member in plan) else 0
        cwd = args.cwd.expanduser().resolve()
        if not cwd.is_dir():
            raise TeamError(f"--cwd is not a directory: {cwd}")
        value = launch(kilix, plan, cwd=cwd, placeholder=placeholder, allow_duplicates=args.allow_duplicates,
                       dry_run=args.dry_run, background=args.background)
        if args.json:
            print(json.dumps({**value, "plan": plan, "note": note}, indent=2, ensure_ascii=False))
        else:
            print_plan(plan, note)
            verb = "would open" if args.dry_run else "opened"
            print(f"{verb} tab {value['tab_title']!r}")
            if args.dry_run:
                for command in value["commands"]:
                    print("  " + shlex.join(command["kilix"]))
        return 0
    except (TeamError, browser.BrowserError, OSError, ValueError) as error:
        print(json.dumps({"status": "error", "reason": str(error)}))
        print(f"claudex_team.py: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
