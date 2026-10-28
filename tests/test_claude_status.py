import json
import subprocess
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import claude_status


def completed(code, stdout="", stderr=""):
    return subprocess.CompletedProcess(["tb"], code, stdout, stderr)


USAGE = (
    "   Settings  Status   Config   Usage   Stats\n"
    "\n"
    "   Current session\n"
    "   ██████████████████████████████████████████████████ 100% used\n"
    "   Resets 9:09pm (America/Los_Angeles)\n"
    "\n"
    "   Current week (all models)\n"
    "   ██████████████████████████████████                 68% used\n"
    "   Resets Oct 30, 5:59pm (America/Los_Angeles)\n"
    "\n"
    "   Current week (Fable)\n"
    "                                                      0% used\n"
    "   Resets Oct 30, 6pm (America/Los_Angeles)\n"
    "\n"
    "   Esc to cancel\n"
)
DIALOG = "   Settings  Status   Config   Usage   Stats\n   Version: 2.1.312\n   Esc to cancel\n"
NOW = datetime(2026, 10, 27, 20, 48, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Session:
    """A scripted tb. The pane starts as a shell unless command is set."""

    def __init__(self, listed=True, command="bash", commands=None, ls_code=0, phase="shell"):
        self.calls = []
        self.listed = listed
        self.ls_code = ls_code
        self.command = command
        self.commands = commands if commands is not None else ["-bash"]
        self.phase = phase
        self.pending = None
        self.rights = 0
        self.screens = {
            "shell": "pleb@plebtop:~/claudex$\n",
            "ready": "❯ Try something\n",
            "dialog": DIALOG,
            "usage": USAGE,
        }

    def run(self, args, timeout=20):
        self.calls.append(list(args))
        if args[0] == "ls":
            text = f"{claude_status.SESSION}  1  0  1s  1m ago\n" if self.listed else "(no tmux sessions)\n"
            return completed(self.ls_code, text)
        if args[0] == "new":
            self.listed = True
            return completed(0, claude_status.SESSION + "\n")
        if args[0] == "show":
            return completed(0, json.dumps({"ok": True, "data": {"panes": [{
                "command": self.command, "pid": 9, "active": True}]}}))
        if args[0] == "type":
            if args[2] == claude_status.CLAUDE_LAUNCH:
                self.command = "claude"
                self.phase = "ready"
            return completed(0)
        if args[0] == "send":
            self.pending = args[2]
            return completed(0)
        if args[0] == "key" and args[2] == "Enter" and self.pending == "/status":
            self.phase = "dialog"
            self.pending = None
            self.rights = 0
            return completed(0)
        if args[0] == "key" and args[2] == "Left":
            self.phase = "dialog"
            self.rights = 1
            return completed(0)
        if args[0] == "key" and args[2] == "Right":
            self.rights += 1
            if self.rights >= 2:
                self.phase = "usage"
            return completed(0)
        if args[0] == "capture":
            screen = self.screens[self.phase]
            if self.pending:
                screen += f"❯ {self.pending}\n"
            return completed(0, screen)
        return completed(1, stderr=f"unexpected {' '.join(args)}")

    def commands_for(self, pid):
        return list(self.commands)


class ParseTests(unittest.TestCase):
    def test_usage_page_maps_session_week_and_model_week(self):
        windows = claude_status.parse_limits(USAGE, now=NOW)
        self.assertEqual([item["name"] for item in windows], ["five_hour", "weekly", "weekly_fable"])
        self.assertEqual([item["used_percent"] for item in windows], [100.0, 68.0, 0.0])
        self.assertEqual([item["remaining_percent"] for item in windows], [0.0, 32.0, 100.0])
        self.assertEqual(windows[1]["label"], "weekly")
        self.assertEqual(windows[2]["label"], "weekly (Fable)")
        zone = ZoneInfo("America/Los_Angeles")
        self.assertEqual(datetime.fromtimestamp(windows[0]["resets_at"], zone).strftime("%Y-%m-%d %H:%M"),
                         "2026-10-27 21:09")
        self.assertEqual(datetime.fromtimestamp(windows[1]["resets_at"], zone).strftime("%Y-%m-%d %H:%M"),
                         "2026-10-30 17:59")
        self.assertEqual(datetime.fromtimestamp(windows[2]["resets_at"], zone).strftime("%Y-%m-%d %H:%M"),
                         "2026-10-30 18:00")

    def test_left_is_converted_and_scrollback_stops_at_the_last_page(self):
        windows = claude_status.parse_limits("Current session\n25% left\n", now=NOW)
        self.assertEqual(windows[0]["used_percent"], 75.0)
        screen = "Settings  Status   Config   Usage   Stats\nCurrent session\n10% used\n" + USAGE
        self.assertNotIn("10% used", claude_status.latest_page(screen))
        self.assertEqual(claude_status.parse_limits(claude_status.latest_page(screen), now=NOW)[0]["used_percent"], 100.0)

    def test_command_match_does_not_treat_claudex_as_claude(self):
        self.assertTrue(claude_status.is_claude_command("claude"))
        self.assertTrue(claude_status.is_claude_command("/usr/bin/claude --help"))
        self.assertFalse(claude_status.is_claude_command("claudex usage"))


class ReadTests(unittest.TestCase):
    def read(self, session):
        clock = Clock()
        return claude_status.read(cwd="/home/pleb/claudex", run=session.run,
                                  commands_for=session.commands_for, clock=clock, sleep=clock.sleep,
                                  now=NOW)

    def test_missing_session_launches_claude_and_opens_usage_with_two_rights(self):
        session = Session(listed=False)
        entry = self.read(session)
        self.assertEqual(entry["status"], "ok")
        self.assertEqual([item["used_percent"] for item in entry["windows"]], [100.0, 68.0, 0.0])
        self.assertIn(["new", "claude_usage", "--cwd", "/home/pleb/claudex"], session.calls)
        self.assertNotIn("a", [call[0] for call in session.calls])
        typed = [call[2] for call in session.calls if call[0] == "type"]
        self.assertEqual(typed, [claude_status.CLAUDE_LAUNCH])
        self.assertEqual([call[2] for call in session.calls if call[0] == "send"], ["/status"])
        enter = next(index for index, call in enumerate(session.calls) if call[:3] == ["key", "claude_usage", "Enter"])
        rights = [index for index, call in enumerate(session.calls) if call[:3] == ["key", "claude_usage", "Right"]]
        self.assertEqual(len(rights), 2)
        self.assertGreater(rights[0], enter)
        last_send = max(index for index, call in enumerate(session.calls) if call[0] == "send")
        self.assertTrue(any(call[0] == "capture" for call in session.calls[last_send + 1:enter]))

    def test_an_open_status_dialog_is_refreshed_with_left_then_right(self):
        session = Session(command="claude", commands=["claude"], phase="usage")
        entry = self.read(session)
        self.assertEqual(entry["status"], "ok")
        self.assertEqual([item["remaining_percent"] for item in entry["windows"]], [0.0, 32.0, 100.0])
        self.assertFalse(any(call[0] in {"type", "send"} for call in session.calls))
        keys = [call[2] for call in session.calls if call[0] == "key"]
        self.assertEqual(keys, ["Left", "Right"])

    def test_a_running_claude_at_the_composer_opens_status_instead_of_pressing_left(self):
        session = Session(command="claude", commands=["claude"], phase="ready")
        entry = self.read(session)
        self.assertEqual(entry["status"], "ok")
        self.assertEqual([call[2] for call in session.calls if call[0] == "send"], ["/status"])
        self.assertNotIn("Left", [call[2] for call in session.calls if call[0] == "key"])
        self.assertFalse(any(call[0] == "type" for call in session.calls))

    def test_a_busy_pane_is_not_overwritten(self):
        session = Session(command="vim", commands=["vim"])
        entry = self.read(session)
        self.assertEqual(entry["status"], "error")
        self.assertIn("vim", entry["reason"])
        self.assertFalse(any(call[0] == "type" for call in session.calls))

    def test_no_tmux_server_still_creates_the_session(self):
        session = Session(listed=False, ls_code=6)
        entry = self.read(session)
        self.assertEqual(entry["status"], "ok")
        self.assertIn(["new", "claude_usage", "--cwd", "/home/pleb/claudex"], session.calls)


if __name__ == "__main__":
    unittest.main()
