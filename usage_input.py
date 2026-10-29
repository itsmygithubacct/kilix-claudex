"""Collect website percentages when an automatic usage reading is unavailable."""
import math
import re
import sys
import time

from providers import CODEX_ID, GROK_ID, LABELS, USAGE_PAGES
from usage_pages import window


class ManualUsageRequired(ValueError):
    pass


def needs_input(entry):
    if not isinstance(entry, dict) or entry.get("status") != "ok":
        return True
    windows = entry.get("windows")
    if not isinstance(windows, list):
        return True
    return not any(isinstance(item, dict) and type(item.get("used_percent")) in (int, float)
                   and math.isfinite(item["used_percent"])
                   and 0 <= item["used_percent"] <= 100 for item in windows)


def percentage(text, basis):
    """Return percent used, accepting either label as printed on a website."""
    if text.strip().lower() in {"unknown", "n/a", "-"}:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%?\s*(used|left|remaining)?\s*", text, re.I)
    if not match or not 0 <= float(match[1]) <= 100:
        raise ValueError("enter a percentage from 0 to 100, optionally followed by used or left")
    value = float(match[1])
    label = (match[2] or basis).lower()
    return value if label == "used" else 100 - value


def read_manual(provider, previous=None, *, now=None, ask=None):
    """Ask only for this provider; prompts use stderr so stdout can stay JSON."""
    now = time.time() if now is None else now
    interactive = ask is None
    if interactive:
        if not sys.stdin.isatty():
            raise ManualUsageRequired(
                f"{LABELS[provider]} usage needs website percentages; run "
                f"claudex usage manual {provider} in a terminal, then retry")
        print(f"Enter {LABELS[provider]} usage from {USAGE_PAGES[provider]}", file=sys.stderr)
        print("Use the displayed percentage. Append 'used' or 'left' to change the label; "
              "enter 'unknown' if a window is not shown.", file=sys.stderr)

        def ask(question):
            print(question, end="", file=sys.stderr, flush=True)
            answer = sys.stdin.readline()
            if not answer:
                raise ManualUsageRequired("usage entry cancelled; no session launched")
            return answer.rstrip("\r\n")

    names = ["five_hour", "weekly"]
    if provider == GROK_ID:
        while True:
            period = ask("Grok window shown [weekly/monthly, default weekly]: ").strip().lower() or "weekly"
            if period in {"weekly", "monthly"}:
                names = [period]
                break
            if interactive:
                print("Enter weekly or monthly.", file=sys.stderr)
    basis = "left" if provider == CODEX_ID else "used"
    windows = []
    for name in names:
        label = "5-hour / current session" if name == "five_hour" else name + " / all models"
        while True:
            try:
                used = percentage(ask(f"{LABELS[provider]} {label}: percent {basis.upper()} (0–100): "), basis)
                break
            except ValueError as error:
                if isinstance(error, ManualUsageRequired):
                    raise
                if interactive:
                    print(str(error), file=sys.stderr)
        if used is not None:
            windows.append(window(name, used))
    if not windows:
        raise ManualUsageRequired(f"no {LABELS[provider]} percentages entered; no session launched")
    return {"status": "ok", "source": "manual website reading", "page": USAGE_PAGES[provider],
            "checked_at": now, "windows": windows}


def fallback(provider, entry, *, now=None):
    return read_manual(provider, entry, now=now) if needs_input(entry) else entry
