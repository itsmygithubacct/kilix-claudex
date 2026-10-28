import json
import subprocess
import unittest

import codex_status


def completed(code, stdout="", stderr=""):
    return subprocess.CompletedProcess(["tb"], code, stdout, stderr)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Session:
    """A scripted tb. The pane starts as a shell unless command is set."""

    def __init__(self, listed=True, command="bash", commands=None, ls_code=0):
        self.calls = []
        self.listed = listed
        self.ls_code = ls_code
        self.command = command
        self.commands = commands if commands is not None else ["-bash"]
        self.phase = "shell"
        self.pending = None
        self.screens = {
            "shell": "pleb@plebtop:~/claudex$\n",
            "ready": "› Implement the task\n gpt-5.3-codex\n",
            "status1": "5-hour limit 10% left\nWeekly limit 10% left\n",
            "status2": "5-hour limit 25% left\nWeekly limit 55% left\nContext window 12% used\n",
        }

    def run(self, args, timeout=20):
        self.calls.append(list(args))
        if args[0] == "ls":
            text = f"{codex_status.SESSION}  1  0  1s  1m ago\n" if self.listed else "(no tmux sessions)\n"
            return completed(self.ls_code, text)
        if args[0] == "new":
            self.listed = True
            return completed(0, codex_status.SESSION + "\n")
        if args[0] == "show":
            return completed(0, json.dumps({"ok": True, "data": {"panes": [{
                "command": self.command, "pid": 9, "active": True}]}}))
        if args[0] == "type":
            if args[2] == codex_status.CODEX_LAUNCH:
                self.command = "codex"
                self.phase = "ready"
            return completed(0)
        if args[0] == "send":
            self.pending = args[2]
            return completed(0)
        if args[0] == "key" and args[2] == "Enter" and self.pending == "/status":
            self.phase = "status2" if self.phase == "status1" else "status1"
            self.pending = None
            return completed(0)
        if args[0] == "capture":
            screen = self.screens[self.phase]
            if self.pending:
                screen += f"› {self.pending}\n"
            return completed(0, screen)
        return completed(1, stderr=f"unexpected {' '.join(args)}")

    def commands_for(self, pid):
        return list(self.commands)


class ParseTests(unittest.TestCase):
    def test_explicit_labels_win_and_context_is_separate(self):
        screen = "primary 90% used\n5-hour limit 25% left\nsecondary 1% used\nWeekly limit 55% left\n"
        windows, context = codex_status.parse_limits(screen + "Context window 12% used\n")
        self.assertEqual([item["name"] for item in windows], ["five_hour", "weekly"])
        self.assertEqual([item["used_percent"] for item in windows], [75.0, 45.0])
        self.assertEqual(context, 12.0)

    def test_scrollback_is_cut_to_the_last_status_card(self):
        screen = ">_ OpenAI Codex (v0.1)\nWeekly limit 10% left\n/status\n>_ OpenAI Codex (v0.1)\nWeekly limit 55% left\n"
        self.assertEqual(codex_status.latest_status(screen), ">_ OpenAI Codex (v0.1)\nWeekly limit 55% left\n")

    def test_the_later_block_is_the_second_status(self):
        windows, _ = codex_status.parse_limits(
            "Weekly limit 10% left (resets 4:13 AM on 2 Nov)\nWeekly limit 55% left\n", now=1800000000.0)
        self.assertEqual(windows[0]["used_percent"], 45.0)
        self.assertIsNone(windows[0]["resets_at"])

    def test_primary_and_secondary_are_guesses(self):
        windows, context = codex_status.parse_limits("primary 10% used\nsecondary 20% left\n")
        self.assertEqual([(item["name"], item["used_percent"]) for item in windows],
                         [("five_hour", 10.0), ("weekly", 80.0)])
        self.assertIsNone(context)

    def test_session_name_matches_a_whole_field(self):
        self.assertTrue(codex_status.session_listed("codex_usage  1  0  1s  1m ago\n"))
        self.assertFalse(codex_status.session_listed("codex_usage_extra  1  0  1s  1m ago\n"))
        self.assertFalse(codex_status.session_listed("(no tmux sessions)\n"))


class ReadTests(unittest.TestCase):
    def read(self, session):
        clock = Clock()
        return codex_status.read(cwd="/home/pleb/claudex", run=session.run,
                                 commands_for=session.commands_for, clock=clock, sleep=clock.sleep,
                                 now=1000.0)

    def test_missing_session_is_created_with_tb_new_and_only_the_second_status_is_kept(self):
        session = Session(listed=False)
        entry = self.read(session)
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["screen"], session.screens["status2"])
        self.assertNotIn("10% left", entry["screen"])
        self.assertEqual([item["used_percent"] for item in entry["windows"]], [75.0, 45.0])
        self.assertEqual(entry["context_used_percent"], 12.0)
        kinds = [call[0] for call in session.calls]
        self.assertIn(["new", "codex_usage", "--cwd", "/home/pleb/claudex"], session.calls)
        self.assertNotIn("a", kinds)
        typed = [call[2] for call in session.calls if call[0] == "type"]
        self.assertEqual(typed, [codex_status.CODEX_LAUNCH])
        self.assertEqual([call[2] for call in session.calls if call[0] == "send"], ["/status", "/status"])
        last_send = None
        for index, call in enumerate(session.calls):
            if call[0] == "send":
                last_send = index
            if call[0] == "key":
                between = session.calls[last_send + 1:index]
                self.assertTrue(any(item[0] == "capture" for item in between))

    def test_a_running_codex_is_not_launched_again(self):
        session = Session(command="codex", commands=["node /usr/local/lib/node_modules/@openai/codex/bin/codex.js"])
        session.phase = "ready"
        entry = self.read(session)
        self.assertEqual(entry["status"], "ok")
        typed = [call[2] for call in session.calls if call[0] == "type"]
        self.assertEqual(typed, [])
        self.assertEqual([call[2] for call in session.calls if call[0] == "send"], ["/status", "/status"])

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
        self.assertIn(["new", "codex_usage", "--cwd", "/home/pleb/claudex"], session.calls)


if __name__ == "__main__":
    unittest.main()
