import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

# Limit detection reads account-wide session files; never let tests see real ones.
import providers  # noqa: E402

os.environ[providers.CODEX_HOME_ENV] = tempfile.mkdtemp(prefix="claudex-test-codex-")
os.environ[providers.COMPANION_CONFIG_ENV] = tempfile.mkdtemp(prefix="claudex-test-companion-")

import pane_loop as loop
from idleness import assess_idle


def pane(activity="idle"):
    return {"pane_id": 42, "activity": activity,
            "broker": {"session_id": "0123456789abcdef"},
            "coding_session": {"state": "live", "provider": "codex", "session_id": "codex-session",
                               "last_turn_event": "task_complete", "last_user_message": ""}}


class FakeClient:
    def __init__(self, activity="idle", show_text=True, submit=True, delete=True):
        self.pane = pane(activity)
        self.text = "› Ask Codex to do anything\nmodel · test"
        self.calls = []
        self.show_text = show_text
        self.submit = submit
        self.delete = delete
        self.pending = ""

    def dump(self, target, lines=80, ansi=False):
        return {"pane": copy.deepcopy(self.pane), "text": self.text}

    def call(self, *args, text=None):
        self.calls.append((args, text))
        if "--enter" in args:
            if self.submit:
                self.pane["coding_session"]["last_user_message"] = self.pending
            self.text = "submitted: " + self.pending + "\n› Ask Codex to do anything\nmodel · test"
        elif text and set(text) == {"\x7f"}:
            if self.delete:
                self.pending = self.pending[:max(0, len(self.pending) - len(text))]
                self.text = ("› " + self.pending if self.pending
                             else "› Ask Codex to do anything") + "\nmodel · test"
        else:
            self.pending = text
            if self.show_text:
                self.text = "› " + text + "\nmodel · test"
        return {"accepted": True}


class TabQueueClient(FakeClient):
    def __init__(self, accept_tab=True):
        super().__init__(activity="working", submit=False)
        self.queued = False
        self.accept_tab = accept_tab

    def dump(self, *args, **kwargs):
        value = super().dump(*args, **kwargs)
        if self.queued:
            value["text"] = ("• Queued follow-up inputs\n  ↳ " + self.pending
                             + "\n› Ask Codex to do anything\nmodel · test")
        elif self.pending:
            value["text"] = ("• Working (4s • esc to interrupt)\n› " + self.pending
                             + "\ntab to queue message\nmodel · test")
        else:
            value["text"] = ("• Working (4s • esc to interrupt)\n"
                             "› Ask Codex to do anything\nmodel · test")
        return value

    def call(self, *args, text=None):
        if text == "\t":
            self.calls.append((args, text))
            self.queued = self.accept_tab
            return {"accepted": True}
        return super().call(*args, text=text)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.runtime = Path(self.folder.name)
        self.assess = patch.object(loop, "assess", side_effect=lambda p, t, allow_busy=False: {
            "ready": p["activity"] == "idle" or (allow_busy and p["activity"] == "working"),
            "reason": p["activity"],
            "screen": {"state": p["activity"]}})
        self.assess.start()

    def tearDown(self):
        self.assess.stop()
        self.folder.cleanup()

    def test_text_then_enter_is_verified_against_recorded_turn(self):
        client = FakeClient()
        result = loop.deliver(client, "42", "a literal $(command) `quoted` prompt", self.runtime)
        self.assertTrue(result["turn_observed"])
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn("--enter", client.calls[0][0])
        self.assertIn("--enter", client.calls[1][0])
        self.assertEqual(client.calls[1][1], "")
        self.assertEqual(client.calls[0][0][1], "0123456789abcdef")
        self.assertIn("$(command) `quoted`", client.calls[0][1])

    def test_busy_tick_does_not_type_or_queue(self):
        client = FakeClient("working")
        result = loop.deliver(client, "42", "continue", self.runtime)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(client.calls, [])

    def test_changed_coding_session_cannot_receive_a_prompt(self):
        client = FakeClient()
        old = loop.identity(client.pane)
        client.pane["coding_session"]["session_id"] = "another-agent"
        with self.assertRaisesRegex(loop.LoopError, "session changed"):
            loop.deliver(client, "42", "continue", self.runtime, expected=old)
        self.assertEqual(client.calls, [])

    def test_allow_busy_still_preserves_a_users_draft(self):
        client = FakeClient("working")
        with patch.object(loop, "assess", return_value={
                "ready": False, "reason": "draft exists", "screen": {"state": "occupied"}}):
            result = loop.deliver(client, "42", "continue", self.runtime, allow_busy=True)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(client.calls, [])

    def test_shared_lock_prevents_two_senders(self):
        client = FakeClient()
        with loop.exclusive(self.runtime / "send-42.lock"):
            with self.assertRaises(loop.Busy):
                loop.deliver(client, "42", "continue", self.runtime)
        self.assertEqual(client.calls, [])

    def test_silent_rejection_never_sends_enter(self):
        client = FakeClient(show_text=False)
        with self.assertRaisesRegex(loop.LoopError, "placement is unverified"):
            loop.deliver(client, "42", "continue", self.runtime)
        self.assertEqual(len(client.calls), 1)

    def test_unobserved_submission_is_not_retried(self):
        client = FakeClient(submit=False)
        with patch.object(loop.time, "sleep"), self.assertRaisesRegex(loop.LoopError, "no matching Codex turn"):
            loop.deliver(client, "42", "continue", self.runtime)
        self.assertEqual(len(client.calls), 2)

    def test_manual_busy_send_can_be_verified_after_tools_return(self):
        client = FakeClient("working", submit=False)
        with patch.object(loop.time, "sleep"):
            result = loop.deliver(client, "42", "continue", self.runtime, allow_busy=True)
        self.assertEqual(result["status"], "pending")
        client.pane["coding_session"]["last_user_message"] = client.pending
        checked = loop.verify_last_send(client, self.runtime)
        self.assertTrue(checked["turn_observed"])
        self.assertEqual(checked["status"], "sent")
        self.assertEqual(len(client.calls), 2)

    def test_multiline_unicode_stays_in_exact_snapshot(self):
        prompt = "  First line\r\n" + "你好" * 600 + "\nLast line\t \n\n"
        message = loop.make_message(prompt, self.runtime, "cx-test")
        self.assertNotIn("\n", message)
        self.assertLessEqual(len(message.encode()), 900)
        self.assertEqual((self.runtime / "prompts/cx-test.txt").read_bytes(), prompt.encode())

    def test_empty_and_terminal_controls_never_type(self):
        for prompt in ("\n  ", "bad\x1b[1A", "\vtrimmed control", "bad\x7f", "bad\x85",
                       "bad\x9b2J", "bad\u202e", "bad\u2028"):
            client = FakeClient()
            with self.assertRaises(loop.LoopError):
                loop.deliver(client, "42", prompt, self.runtime)
            self.assertEqual(client.calls, [])

    def test_rejected_enter_is_an_error_even_for_manual_delivery(self):
        class RejectedEnter(FakeClient):
            def call(self, *args, text=None):
                accepted = super().call(*args, text=text)
                return {"accepted": False} if "--enter" in args else accepted

        client = RejectedEnter(submit=False)
        with patch.object(loop.time, "sleep"), self.assertRaisesRegex(loop.LoopError, "accept Enter"):
            loop.deliver(client, "42", "continue", self.runtime, allow_busy=True)
        self.assertEqual(len(client.calls), 2)

    def test_visible_tail_without_the_body_never_submits(self):
        class LostBody(FakeClient):
            def dump(self, *args, **kwargs):
                value = super().dump(*args, **kwargs)
                if self.pending:
                    value["text"] = self.pending[self.pending.rfind("["):]
                return value

        client = LostBody()
        with self.assertRaisesRegex(loop.LoopError, "placement is unverified"):
            loop.deliver(client, "42", "Do not remove the tests", self.runtime)
        self.assertEqual(len(client.calls), 1)

    def test_braille_dust_over_spaces_does_not_hide_a_complete_message(self):
        class DustClient(FakeClient):
            def dump(self, *args, **kwargs):
                value = super().dump(*args, **kwargs)
                value["text"] = value["text"].replace(" ", "⠄")
                return value

        result = loop.deliver(DustClient(), "42", "do NOT edit the tests", self.runtime)
        self.assertTrue(result["turn_observed"])
        self.assertFalse(loop.message_visible("do NOT edit", "› do ⠄ edit\nmodel"))
        self.assertFalse(loop.message_visible("literal ⠓ data", "› literal data\nmodel"))
        self.assertTrue(loop.message_visible("literal ⠓ data", "› literal ⠓ data\nmodel"))

    def test_verify_cannot_overwrite_a_newer_send(self):
        client = FakeClient(submit=False)
        with patch.object(loop.time, "sleep"):
            first = loop.deliver(client, "42", "first", self.runtime, allow_busy=True)

        class SendDuringVerify(FakeClient):
            def dump(inner, *args, **kwargs):
                loop.deliver(FakeClient(), "42", "second", self.runtime)
                return super().dump(*args, **kwargs)

        checked = loop.verify_last_send(SendDuringVerify(), self.runtime)
        latest = json.loads((self.runtime / "last-send.json").read_text())
        self.assertEqual(checked["token"], first["token"])
        self.assertFalse(checked["is_latest"])
        self.assertNotEqual(latest["token"], first["token"])

    def test_prompt_file_reference_never_types_a_control_in_its_path(self):
        client = FakeClient()
        with self.assertRaisesRegex(loop.LoopError, "file path"):
            loop.deliver(client, "42", "multiple\nlines", self.runtime / "bad\npath")
        self.assertEqual(client.calls, [])

    def test_concurrent_verifiers_keep_confirmed_turn_evidence(self):
        client = FakeClient(submit=False)
        with patch.object(loop.time, "sleep"):
            sent = loop.deliver(client, "42", "continue", self.runtime, allow_busy=True)
        observer = FakeClient()
        observer.pane["coding_session"]["last_user_message"] = sent["message"]

        class SlowVerifier(FakeClient):
            def dump(inner, *args, **kwargs):
                loop.verify_last_send(observer, self.runtime)
                return super().dump(*args, **kwargs)

        checked = loop.verify_last_send(SlowVerifier(), self.runtime)
        self.assertTrue(checked["turn_observed"])
        self.assertEqual(checked["status"], "sent")
        self.assertTrue(json.loads((self.runtime / "last-send.json").read_text())["turn_observed"])

    def test_busy_codex_uses_tab_and_confirms_queue_without_retry(self):
        client = TabQueueClient()
        with patch.object(loop, "assess", side_effect=assess_idle), patch.object(loop.time, "sleep"):
            result = loop.deliver(client, "42", "finish the current review", self.runtime, allow_busy=True)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["submission_key"], "Tab")
        self.assertTrue(result["queue_observed"])
        self.assertFalse(result["turn_observed"])
        self.assertTrue(result["screen_changed_after_submit"])
        self.assertIsNone(result["screen_changed_after_enter"])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[-1][1], "\t")
        self.assertNotIn("--enter", client.calls[-1][0])
        self.assertEqual(client.calls[-1][0][1], "0123456789abcdef")

    def test_silent_tab_does_not_claim_success_or_send_another_key(self):
        client = TabQueueClient(accept_tab=False)
        with patch.object(loop, "assess", side_effect=assess_idle), patch.object(loop.time, "sleep"), \
                self.assertRaisesRegex(loop.LoopError, "did not visibly submit or queue"):
            loop.deliver(client, "42", "finish the current review", self.runtime, allow_busy=True)
        self.assertEqual(len(client.calls), 2)
        self.assertFalse(json.loads((self.runtime / "last-send.json").read_text())["queue_observed"])

    def test_activity_change_after_placement_withdraws_or_stops_without_submitting(self):
        # Review repro: the verified text used to stay stranded in the composer.
        for activity in ("working", "waiting"):
            with self.subTest(activity=activity):
                class ChangedDuringPlacement(FakeClient):
                    def call(self, *args, text=None):
                        value = super().call(*args, text=text)
                        self.pane["activity"] = activity
                        return value

                client = ChangedDuringPlacement()
                if activity == "working":
                    result = loop.deliver(client, "42", "continue", self.runtime)
                    self.assertEqual((result["status"], result["withdrawn"], result["text_may_remain"]),
                                     ("skipped", True, False))
                    self.assertEqual(len(client.calls), 2)
                    self.assertEqual(set(client.calls[1][1]), {"\x7f"})
                    self.assertEqual(len(client.calls[1][1]), len(result["message"]))
                    self.assertEqual(client.pending, "")
                    self.assertFalse((self.runtime / "last-send.json").exists())
                else:
                    # A dialog may own the keyboard: no deletion keys either.
                    with self.assertRaisesRegex(loop.LoopError, "before submission"):
                        loop.deliver(client, "42", "continue", self.runtime)
                    self.assertEqual(len(client.calls), 1)

    def test_unverified_withdrawal_reports_that_text_may_remain(self):
        class TurnsBusy(FakeClient):
            def call(self, *args, text=None):
                value = super().call(*args, text=text)
                self.pane["activity"] = "working"
                return value

        client = TurnsBusy(delete=False)
        with self.assertRaises(loop.DeliveryError) as caught:
            loop.deliver(client, "42", "continue", self.runtime)
        evidence = caught.exception.evidence
        self.assertEqual(evidence["status"], "uncertain")
        self.assertTrue(evidence["withdraw_attempted"])
        self.assertTrue(evidence["text_may_remain"])
        self.assertFalse(evidence["key_attempted"])
        self.assertEqual(len(client.calls), 2)

    def test_submission_readback_uses_a_styled_capture(self):
        class RecordsCaptures(FakeClient):
            def __init__(self):
                super().__init__()
                self.ansi_flags = []

            def dump(self, target, lines=80, ansi=False):
                self.ansi_flags.append(ansi)
                return super().dump(target, lines, ansi)

        client = RecordsCaptures()
        loop.deliver(client, "42", "review", self.runtime)
        self.assertTrue(client.ansi_flags[-1])

    def test_unexpected_errors_still_return_json_with_the_right_exit_code(self):
        class BreaksBeforeTyping(FakeClient):
            def dump(self, *args, **kwargs):
                raise KeyError("boom")

        class BreaksAfterTyping(FakeClient):
            def dump(self, *args, **kwargs):
                if self.pending:
                    raise KeyError("boom")
                return super().dump(*args, **kwargs)

        for client, status, state in ((BreaksBeforeTyping(), 1, "error"),
                                      (BreaksAfterTyping(), 3, "uncertain")):
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(loop, "Kilix", return_value=client), \
                    redirect_stdout(output), redirect_stderr(errors):
                code = loop.main(["--config", str(self.runtime / "loop.json"), "send", "42", "review"])
            self.assertEqual(code, status)
            self.assertEqual(json.loads(output.getvalue())["status"], state)

    def test_exhausted_or_low_targets_are_skipped_before_typing(self):
        exhausted = {"state": "exhausted", "reason": "weekly 100% used", "windows": {}}
        low = {"state": "low", "reason": "weekly 97% used", "windows": {}}
        with patch.object(loop, "assess_limits", return_value=exhausted):
            client = FakeClient()
            result = loop.deliver(client, "42", "review", self.runtime)
            self.assertEqual(result["status"], "skipped")
            self.assertIn("token limit exhausted", result["reason"])
            self.assertEqual(client.calls, [])
            forced = FakeClient()
            self.assertEqual(loop.deliver(forced, "42", "review", self.runtime, allow_limited=True)["status"], "sent")
        with patch.object(loop, "assess_limits", return_value=low):
            client = FakeClient()
            self.assertEqual(loop.deliver(client, "42", "review", self.runtime)["status"], "sent")
            client = FakeClient()
            result = loop.deliver(client, "42", "review", self.runtime, min_remaining_percent=5)
            self.assertEqual((result["status"], client.calls), ("skipped", []))

    def test_unknown_limits_never_block(self):
        client = FakeClient()
        self.assertEqual(loop.deliver(client, "42", "review", self.runtime)["status"], "sent")

    def test_limits_command_reports_json_and_exits_2_when_exhausted(self):
        client = FakeClient()
        for state, code in (("exhausted", 2), ("low", 0), ("unknown", 0)):
            output = io.StringIO()
            with patch.object(loop, "Kilix", return_value=client), \
                    patch.object(loop, "assess_limits", return_value={"state": state, "reason": state}), \
                    redirect_stdout(output), redirect_stderr(io.StringIO()):
                status = loop.main(["--config", str(self.runtime / "loop.json"), "limits", "42"])
            report = json.loads(output.getvalue())
            self.assertEqual((status, report["status"], report["panes"][0]["pane_id"]), (code, state, 42))
        self.assertEqual(client.calls, [])

    def test_verify_rejects_a_malformed_record_with_json(self):
        # Review repro: a malformed record ended in a KeyError traceback.
        runtime = self.runtime / ".loop"
        runtime.mkdir()
        for content in ("{}", "[]", "not json", '{"pane_id": "42", "token": "cx-1"}'):
            (runtime / "last-send.json").write_text(content)
            output, errors = io.StringIO(), io.StringIO()
            client = FakeClient()
            with patch.object(loop, "Kilix", return_value=client), \
                    redirect_stdout(output), redirect_stderr(errors):
                code = loop.main(["--config", str(self.runtime / "loop.json"), "verify"])
            self.assertEqual(code, 1, content)
            self.assertEqual(json.loads(output.getvalue())["status"], "error")
            self.assertEqual(client.calls, [])

    def test_user_draft_appearing_during_placement_is_never_submitted(self):
        class DraftRace(FakeClient):
            def call(self, *args, text=None):
                return super().call(*args, text="user draft " + text)

        client = DraftRace()
        with self.assertRaises(loop.DeliveryError) as caught:
            loop.deliver(client, "42", "continue", self.runtime)
        self.assertEqual(len(client.calls), 1)
        evidence = caught.exception.evidence
        self.assertEqual(evidence["status"], "uncertain")
        self.assertTrue(evidence["text_may_remain"])
        self.assertFalse(evidence["key_attempted"])
        self.assertEqual(json.loads((self.runtime / "last-send.json").read_text())["token"], evidence["token"])

    def test_latest_queue_hint_wins_over_lagging_activity(self):
        class StartsWorkingOnText(TabQueueClient):
            def dump(self, *args, **kwargs):
                value = super().dump(*args, **kwargs)
                if not self.pending:
                    value["text"] = "› Ask Codex to do anything\nmodel · test"
                return value

        for allow_busy in (False, True):
            with self.subTest(allow_busy=allow_busy):
                client = StartsWorkingOnText()
                client.pane["activity"] = "idle"
                if allow_busy:
                    with patch.object(loop, "assess", side_effect=assess_idle), patch.object(loop.time, "sleep"):
                        result = loop.deliver(client, "42", "review", self.runtime, allow_busy=True)
                    self.assertEqual(result["submission_key"], "Tab")
                    self.assertTrue(result["queue_observed"])
                else:
                    # The initial metadata and mocked screen verdict are idle;
                    # the real hint appears only after the text was placed, so
                    # the verified text is withdrawn instead of left behind.
                    with patch.object(loop, "assess", side_effect=assess_idle):
                        result = loop.deliver(client, "42", "review", self.runtime)
                    self.assertEqual((result["status"], result["withdrawn"]), ("skipped", True))
                    self.assertEqual(len(client.calls), 2)
                    self.assertEqual(set(client.calls[1][1]), {"\x7f"})

    def test_a_quoted_delivery_marker_is_not_evidence_for_the_original_message(self):
        client = FakeClient(submit=False)
        with patch.object(loop.time, "sleep"):
            sent = loop.deliver(client, "42", "review", self.runtime, allow_busy=True)
        for text in ("quoted " + sent["token"] + " elsewhere",
                     "another message [" + sent["token"] + "]"):
            client.pane["coding_session"]["last_user_message"] = text
            self.assertFalse(loop.verify_last_send(client, self.runtime)["turn_observed"])
        client.pane["coding_session"]["last_user_message"] = sent["message"]
        verified = loop.verify_last_send(client, self.runtime)
        self.assertEqual(verified["observed_message"], sent["message"])
        client.pane["coding_session"]["last_user_message"] = "a newer turn"
        self.assertTrue(loop.verify_last_send(client, self.runtime)["turn_observed"])

    def test_command_prefixes_and_literal_braille_use_exact_files(self):
        for index, prompt in enumerate(("/quit", "!remove build", "@notes.txt", "?", "#remember", "⠓⠑⠇⠇⠕")):
            token = f"cx-test{index}"
            message = loop.make_message(prompt, self.runtime, token)
            self.assertTrue(message.startswith("Read and follow"))
            self.assertEqual((self.runtime / "prompts" / f"{token}.txt").read_bytes(), prompt.encode())

    def test_cli_reports_late_delivery_failure_as_json_and_verify_can_recover(self):
        client = FakeClient(submit=False)
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(loop, "Kilix", return_value=client), patch.object(loop.time, "sleep"), \
                redirect_stdout(output), redirect_stderr(errors):
            status = loop.main(["--config", str(self.runtime / "loop.json"), "send", "42", "review"])
        self.assertEqual(status, 3)
        evidence = json.loads(output.getvalue())
        self.assertEqual(evidence["status"], "uncertain")
        self.assertTrue(evidence["text_attempted"])
        self.assertTrue(evidence["key_accepted"])
        self.assertEqual(len(client.calls), 2)
        client.pane["coding_session"]["last_user_message"] = client.pending
        self.assertTrue(loop.verify_last_send(client, self.runtime / ".loop")["turn_observed"])

    def test_cli_lock_and_missing_pane_fail_without_typing_and_return_json(self):
        runtime = self.runtime / ".loop"
        client = FakeClient()
        for missing in (False, True):
            output, errors = io.StringIO(), io.StringIO()
            if missing:
                client.pane = None
            with loop.exclusive(runtime / "send-42.lock"), patch.object(loop, "Kilix", return_value=client), \
                    redirect_stdout(output), redirect_stderr(errors):
                status = loop.main(["--config", str(self.runtime / "loop.json"), "send", "42", "review"])
            self.assertEqual(status, 1 if missing else 2)
            self.assertFalse(json.loads(output.getvalue())["text_attempted"])
            self.assertEqual(client.calls, [])


class DashboardCLITests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.config = Path(self.folder.name) / "loop.json"

    def tearDown(self):
        self.folder.cleanup()

    def run_main(self, *argv):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            status = loop.main(["--config", str(self.config), *argv])
        return status, json.loads(output.getvalue())

    def fake_run(self, calls, stdout=""):
        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
        return run

    def test_provider_pages_open_in_the_desktop_browser_without_a_tab(self):
        calls = []
        with patch.object(loop.subprocess, "run", side_effect=self.fake_run(calls)), \
                patch.object(loop.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"
                             if name == "google-chrome" else None):
            status, value = self.run_main("dashboard", "provider", "all")
        self.assertEqual(status, 0)
        self.assertEqual([c[-1] for c in calls], list(providers.USAGE_PAGES.values()))
        self.assertEqual(calls[0][:4], ["kilix", "@", "launch", "--type=background"])
        self.assertEqual(calls[0][-3:-1], ["kilix", "open-url"])
        self.assertEqual((value["opened"][0]["mode"], value["opened"][0]["browser"]),
                         ("desktop browser", "/usr/bin/google-chrome"))

    def test_without_a_desktop_browser_pages_render_in_a_titled_kilix_tab(self):
        calls = []
        with patch.object(loop.subprocess, "run", side_effect=self.fake_run(calls, stdout="17\n")), \
                patch.object(loop.shutil, "which", return_value=None):
            status, value = self.run_main("dashboard", "provider", providers.COMPANION_ID)
        self.assertEqual(status, 0)
        self.assertEqual(calls[0][:5], ["kilix", "@", "launch", "--type=tab", f"--title={providers.COMPANION_ID}-usage"])
        self.assertEqual(calls[0][-3:], ["kilix", "open-url", providers.USAGE_PAGES[providers.COMPANION_ID]])
        self.assertEqual((value["opened"][0]["mode"], value["opened"][0]["window_id"]), ("kilix pane", "17"))

    def test_open_starts_the_dashboard_then_launches_a_browser_tab(self):
        with patch.object(loop, "start_dashboard", return_value={"running": True, "url": "http://127.0.0.1:9/"}), \
                patch.object(loop, "launch_browser", return_value={"url": "http://127.0.0.1:9/"}) as launch:
            status, value = self.run_main("dashboard", "open", "--port", "9")
        self.assertEqual(status, 0)
        launch.assert_called_once_with("http://127.0.0.1:9/", "claudex-limits")
        self.assertTrue(value["dashboard"]["running"])

    def test_invalid_port_and_launch_failure_are_json_errors(self):
        status, value = self.run_main("dashboard", "start", "--port", "70000")
        self.assertEqual((status, value["status"]), (1, "error"))
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="no kilix here")
        with patch.object(loop.subprocess, "run", return_value=failed):
            status, value = self.run_main("dashboard", "provider", "codex")
        self.assertEqual((status, value["reason"]), (1, "no kilix here"))

    def test_background_dashboard_lifecycle(self):
        import socket
        import urllib.request
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        script = str(Path(loop.__file__))

        def cli(*argv):
            done = subprocess.run([sys.executable, "-B", script, "--config", str(self.config), *argv],
                                  capture_output=True, text=True, env=env, timeout=60)
            return done.returncode, json.loads(done.stdout)

        status, started = cli("dashboard", "start", "--port", str(port))
        try:
            self.assertEqual(status, 0, started)
            self.assertTrue(started["running"])
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/", timeout=5) as page:
                self.assertIn(b"claudex token limits", page.read())
            self.assertTrue(cli("dashboard", "status")[1]["running"])
            self.assertTrue(cli("dashboard", "start", "--port", str(port))[1]["already_running"])
        finally:
            status, stopped = cli("dashboard", "stop")
        self.assertEqual((status, stopped["running"]), (0, False))
        self.assertFalse(cli("dashboard", "status")[1]["running"])


class OutputTests(unittest.TestCase):
    def test_json_output_is_utf8_even_with_an_ascii_stdout(self):
        # Review repro: `loop status` failed with an encoding error under PYTHONIOENCODING=ascii.
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder) / ".loop"
            runtime.mkdir()
            (runtime / "state.json").write_text(json.dumps({"running": False, "target": "café"}),
                                                encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="ascii", PYTHONDONTWRITEBYTECODE="1")
            done = subprocess.run([sys.executable, "-B", str(Path(loop.__file__)), "--config",
                                   str(Path(folder) / "loop.json"), "loop", "status"],
                                  capture_output=True, env=env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout.decode("utf-8"))["target"], "café")


class TimerTests(unittest.TestCase):
    def test_min_remaining_percent_is_optional_and_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "loop.json"
            config = {"target": "42", "prompt_file": "prompt.txt", "enabled": False, "interval_minutes": 5}
            loop.write_json(path, config)
            self.assertNotIn("min_remaining_percent", loop.load_config(path))
            for good in (0, 5, 100):
                loop.write_json(path, {**config, "min_remaining_percent": good})
                self.assertEqual(loop.load_config(path)["min_remaining_percent"], good)
            for bad in (-1, 101, True, "5", float("nan")):
                loop.write_json(path, {**config, "min_remaining_percent": bad})
                with self.assertRaises(loop.LoopError, msg=bad):
                    loop.load_config(path)

    def test_interval_has_a_floor_in_config_and_cli(self):
        # Review repro: `loop every 1e-6` was accepted.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "loop.json"
            config = {"target": "42", "prompt_file": "prompt.txt", "enabled": False}
            loop.write_json(path, {**config, "interval_minutes": 0.001})
            with self.assertRaisesRegex(loop.LoopError, "at least"):
                loop.load_config(path)
            loop.write_json(path, {**config, "interval_minutes": loop.MIN_INTERVAL_MINUTES})
            self.assertEqual(loop.load_config(path)["interval_minutes"], loop.MIN_INTERVAL_MINUTES)
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(loop.main(["--config", str(path), "loop", "every", "1e-6"]), 1)
            self.assertEqual(json.loads(output.getvalue())["status"], "error")
            self.assertEqual(loop.load_config(path)["interval_minutes"], loop.MIN_INTERVAL_MINUTES)

    def test_transient_read_and_invalid_prompt_skip_without_killing_the_loop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config_path = root / "loop.json"
            loop.write_json(config_path, {"target": "42", "interval_minutes": 0.01,
                                          "prompt_file": "prompt.txt", "enabled": True})
            (root / "prompt.txt").write_text("bad\x1b[1A")
            now = [0.0]

            class ClockEvent:
                def is_set(self):
                    return now[0] >= 2

                def set(self):
                    now[0] = 2

                def wait(self, seconds):
                    now[0] += seconds
                    if now[0] > 1.5:
                        (root / "prompt.txt").write_text("valid prompt")

            class FirstTickTimeout(FakeClient):
                dumps = 0

                def dump(self, *args, **kwargs):
                    self.dumps += 1
                    if self.dumps == 2:
                        raise loop.LoopError("temporary dump timeout")
                    return super().dump(*args, **kwargs)

            client = FirstTickTimeout()
            output = io.StringIO()
            with patch.object(loop.threading, "Event", ClockEvent), \
                    patch.object(loop.time, "monotonic", side_effect=lambda: now[0]), \
                    patch.object(loop.signal, "signal"), redirect_stdout(output):
                loop.run_loop(config_path, client, root / ".loop")
            self.assertIn("temporary dump timeout", output.getvalue())
            self.assertIn("terminal control characters", output.getvalue())
            self.assertEqual(len(client.calls), 2)
            self.assertIn("valid prompt", client.calls[0][1])
            self.assertEqual(loop.read_state(root / ".loop")["phase"], "stopped")

    def test_running_timer_reloads_interval_and_prompt_and_skips_busy_tick(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config_path = root / "loop.json"
            config = {"target": "42", "interval_minutes": 0.1, "prompt_file": "prompt.txt", "enabled": True}
            loop.write_json(config_path, config)
            (root / "prompt.txt").write_text("original")
            now = [0.0]
            ticks = []

            class ClockEvent:
                def is_set(self):
                    return now[0] >= 7

                def set(self):
                    now[0] = 7

                def wait(self, seconds):
                    now[0] += seconds
                    if now[0] == 2:
                        config["interval_minutes"] = 0.05
                        loop.write_json(config_path, config)
                        (root / "prompt.txt").write_text("first edit")
                    if now[0] == 4:
                        (root / "prompt.txt").write_text("second edit")

            def deliver(client, target, prompt, *args, **kwargs):
                ticks.append((now[0], prompt))
                return ({"status": "skipped", "reason": "working"} if len(ticks) == 1
                        else {"status": "sent", "sent_at": now[0]})

            with patch.object(loop.threading, "Event", ClockEvent), \
                    patch.object(loop.time, "monotonic", side_effect=lambda: now[0]), \
                    patch.object(loop.time, "time", side_effect=lambda: now[0]), \
                    patch.object(loop.signal, "signal"), patch.object(loop, "deliver", side_effect=deliver):
                loop.run_loop(config_path, FakeClient(), root / ".loop")
            self.assertEqual(ticks, [(3.0, "first edit"), (6.0, "second edit")])
            state = json.loads((root / ".loop/state.json").read_text())
            self.assertFalse(state["running"])
            self.assertEqual(state["last_fire_at"], 6)
            self.assertIsNone(state["next_fire_at"])

    def test_invalid_interval_cannot_enable_a_fast_loop(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "loop.json"
            for minutes in (True, 0, -1, float("nan"), float("inf"), "1"):
                loop.write_json(path, {"target": "42", "interval_minutes": minutes,
                                       "prompt_file": "prompt.txt", "enabled": True})
                with self.assertRaises(loop.LoopError):
                    loop.load_config(path)

    def test_stale_pid_is_reported_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            loop.write_json(runtime / "state.json", {"running": True, "pid": 123,
                                                     "process_start": "old"})
            with patch.object(loop, "process_start", return_value="new"):
                self.assertFalse(loop.read_state(runtime)["running"])

    def test_malformed_state_cannot_claim_a_live_process_or_signal_one(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            records = ([], None, "bad state",
                       {"running": True, "pid": 42},
                       {"running": True, "pid": 42, "process_start": None},
                       {"running": True, "pid": 42, "process_start": ""},
                       {"running": True, "pid": True, "process_start": "123"},
                       {"running": True, "pid": -42, "process_start": "123"},
                       {"running": "true", "pid": 42, "process_start": "123"})
            for record in records:
                with self.subTest(record=record):
                    loop.write_json(runtime / "state.json", record)
                    with patch.object(loop, "process_start", return_value=None), \
                            patch.object(loop.os, "pidfd_open") as signal_target:
                        self.assertFalse(loop.read_state(runtime)["running"])
                        self.assertFalse(loop.stop_loop(runtime)["running"])
                        signal_target.assert_not_called()
            loop.write_json(runtime / "state.json", {
                "running": True, "pid": 42, "process_start": "123"})
            with patch.object(loop, "process_start", return_value="123"):
                self.assertTrue(loop.read_state(runtime)["running"])
            with patch.object(loop, "process_start", return_value=None):
                self.assertFalse(loop.read_state(runtime)["running"])


# Exercise the actual CLI/daemon lifecycle without typing into a real terminal.
# This fixture emulates only the external Pane Center and screen-capture boundary.
FAKE_KILIX = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ["CLAUDEX_FAKE_DIR"])
path = root / "pane.json"
record = json.loads(path.read_text())
args = sys.argv[1:]
pending = record.get("pending", "")
hint = "\x1b[2mAsk Codex to do anything\x1b[22m"
body = pending or hint
screen = "\n".join([record.get("history", ""), "─ Worked for 1s ─", "› " + body, "gpt-6-astra · test", ""])
if args[0] == "@":
    print(screen)
elif args[0] == "dump":
    print(json.dumps({"pane": record["pane"], "text": screen.replace("\x1b[2m", "").replace("\x1b[22m", "")}))
elif args[0] == "send":
    text = sys.stdin.read()
    if "--enter" in args:
        record["history"] = pending
        record["pane"]["coding_session"]["last_user_message"] = pending
        record["pending"] = ""
        with (root / "submitted.jsonl").open("a") as stream:
            stream.write(json.dumps(pending) + "\n")
    else:
        record["pending"] = pending + text
    temporary = root / ("pane." + str(os.getpid()) + ".tmp")
    temporary.write_text(json.dumps(record))
    temporary.replace(path)
    print(json.dumps({"accepted": True}))
else:
    raise SystemExit(1)
'''


class CLILifecycleTests(unittest.TestCase):
    def test_init_creates_private_paused_data_and_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data_home = root / "gpu-data"
            app = data_home / "kilix-apps/kilix-claudex"
            env = {**os.environ, "GPU_TERMINAL_HOME": str(data_home), "CLAUDEX_CONFIG": "",
                   "CLAUDEX_KILIX_PANES": str(root / "absent-kilix")}
            program = [sys.executable, "-B", str(Path(loop.__file__).resolve())]

            def cli(*args, content=None):
                return subprocess.run([*program, *args], input=content, env=env, cwd=root,
                                      capture_output=True, timeout=10)

            first = cli("init")
            self.assertEqual(first.returncode, 0, first.stderr)
            value = json.loads(first.stdout)
            self.assertTrue(value["config_created"])
            self.assertTrue(value["prompt_created"])
            self.assertFalse(value["enabled"])
            self.assertEqual(value["config_file"], str(app / "loop.json"))
            for path in (data_home, data_home / "kilix-apps", app, app / ".loop"):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700, path)
            for path in (app / "loop.json", app / "prompt.txt", app / ".loop/config.lock"):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)
            self.assertFalse((app / ".loop/state.json").exists())
            self.assertFalse((root / ".loop").exists())

            content = b"  preserve CRLF\r\n\tand trailing spaces  \r\n\r\n"
            edited = cli("loop", "prompt", "--file", "-", content=content)
            self.assertEqual(edited.returncode, 0, edited.stderr)
            self.assertEqual((app / "prompt.txt").read_bytes(), content)
            self.assertEqual(cli("loop", "prompt").stdout, content)
            config_bytes = (app / "loop.json").read_bytes()
            second = cli("init")
            self.assertEqual(second.returncode, 0, second.stderr)
            value = json.loads(second.stdout)
            self.assertFalse(value["config_created"])
            self.assertFalse(value["prompt_created"])
            self.assertEqual((app / "prompt.txt").read_bytes(), content)
            self.assertEqual((app / "loop.json").read_bytes(), config_bytes)

    def test_init_honors_custom_configuration_and_keeps_invalid_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "custom.json"
            loop.write_json(config, {"target": "42", "interval_minutes": 2,
                                     "prompt_file": "notes/instructions.txt", "enabled": True})
            original = config.read_bytes()
            value = loop.initialize(config, root / ".loop")
            self.assertFalse(value["config_created"])
            self.assertTrue(value["prompt_created"])
            self.assertTrue(value["enabled"])
            self.assertTrue((root / "notes/instructions.txt").is_file())
            self.assertFalse((root / "prompt.txt").exists())
            self.assertEqual(config.read_bytes(), original)
            config.write_bytes(b"invalid user configuration")
            with self.assertRaises(ValueError):
                loop.initialize(config, root / ".loop")
            self.assertEqual(config.read_bytes(), b"invalid user configuration")

    def test_prompt_file_replacement_preserves_utf8_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "loop.json"
            loop.write_json(config, {"target": "42", "interval_minutes": 5,
                                     "prompt_file": "prompt.txt", "enabled": False})
            content = "  keep indentation\r\n你好\t  \r\n\r\n".encode()
            incoming = root / "incoming.txt"
            incoming.write_bytes(content)
            with redirect_stdout(io.StringIO()):
                result = loop.main(["--config", str(config), "loop", "prompt", "--file", str(incoming)])
            self.assertEqual(result, 0)
            self.assertEqual((root / "prompt.txt").read_bytes(), content)

    def test_default_config_uses_private_app_data_and_resolves_shared_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / "private-workspace" / "loop.json"
            original.parent.mkdir()
            original.write_text("{}")
            data_home = root / "gpu-data"
            link = data_home / "kilix-apps" / "kilix-claudex" / "loop.json"
            env = {**os.environ, "GPU_TERMINAL_HOME": str(data_home)}
            env.pop("CLAUDEX_CONFIG", None)
            program = [sys.executable, "-B", str(Path(loop.__file__).resolve())]
            fresh = subprocess.run([*program, "loop", "status"], env=env,
                                   cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(fresh.returncode, 0, fresh.stderr)
            self.assertEqual(json.loads(fresh.stdout)["config_file"], str(link))
            with patch.dict(os.environ, {"GPU_TERMINAL_HOME": "", "CLAUDEX_CONFIG": ""}), \
                    patch.object(loop.Path, "home", return_value=root):
                self.assertEqual(loop.default_config(), root / ".local/gpu_terminal/kilix-apps/kilix-claudex/loop.json")
            link.parent.mkdir(parents=True)
            link.symlink_to(original)
            loop.write_json(original.parent / ".loop" / "state.json", {"phase": "paused"})
            result = subprocess.run([*program, "loop", "status"], env=env,
                                    cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(value["config_file"], str(original))
            self.assertEqual(value["phase"], "paused")
            self.assertFalse((Path(loop.__file__).resolve().parent / ".loop").exists())
            env["CLAUDEX_CONFIG"] = str(root / "override.json")
            result = subprocess.run([*program, "loop", "status"], env=env,
                                    cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(json.loads(result.stdout)["config_file"], env["CLAUDEX_CONFIG"])
            result = subprocess.run([*program, "--config", str(link), "loop", "status"],
                                    env=env, cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(json.loads(result.stdout)["config_file"], str(original))

    def test_private_runtime_permissions_on_first_write(self):
        with tempfile.TemporaryDirectory() as folder:
            data_home = Path(folder)
            app = data_home / "kilix-apps" / "kilix-claudex"
            app.mkdir(mode=0o700, parents=True)
            config = app / "loop.json"
            loop.write_json(config, {"target": "42", "interval_minutes": 5,
                                     "prompt_file": "prompt.txt", "enabled": True})
            env = {**os.environ, "GPU_TERMINAL_HOME": str(data_home)}
            env.pop("CLAUDEX_CONFIG", None)
            result = subprocess.run([sys.executable, "-B", str(Path(loop.__file__).resolve()),
                                     "loop", "pause"], env=env, capture_output=True,
                                    text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((app / ".loop").stat().st_mode & 0o777, 0o700)
            for path in (config, app / ".loop/config.lock"):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_detached_loop_pause_reload_duplicate_guard_and_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("kilix", "kilix-panes"):
                program = root / name
                program.write_text(FAKE_KILIX)
                program.chmod(0o700)
            loop.write_json(root / "pane.json", {"pane": pane()})
            config_path = root / "loop.json"
            loop.write_json(config_path, {"target": "42", "interval_minutes": 0.02,
                                          "prompt_file": "prompt.txt", "enabled": False})
            (root / "prompt.txt").write_text("original")
            environment = {**os.environ, "CLAUDEX_FAKE_DIR": str(root),
                           "CLAUDEX_KILIX_PANES": str(root / "kilix-panes"),
                           "PATH": str(root) + os.pathsep + os.environ.get("PATH", "")}

            def cli(*args):
                return subprocess.run([sys.executable, str(Path(loop.__file__)), "--config", str(config_path),
                                       "loop", *args], env=environment, capture_output=True, text=True, timeout=45)

            def wait_for(predicate):
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(0.05)
                self.fail("CLI condition timed out: " + (root / ".loop/loop.log").read_text())

            def submitted():
                path = root / "submitted.jsonl"
                return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

            try:
                result = cli("start")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(json.loads(result.stdout)["running"])
                self.assertEqual(cli("start").returncode, 1)
                self.assertEqual(cli("once").returncode, 2)
                wait_for(lambda: loop.read_state(root / ".loop").get("phase") == "paused")
                self.assertEqual(submitted(), [])
                self.assertEqual(cli("prompt", "edited before first tick").returncode, 0)
                self.assertEqual(cli("resume").returncode, 0)
                wait_for(lambda: len(submitted()) >= 1)
                self.assertIn("edited before first tick", submitted()[0])
                self.assertEqual(cli("pause").returncode, 0)
                wait_for(lambda: loop.read_state(root / ".loop").get("phase") == "paused")
                self.assertEqual(cli("prompt", "edited while paused").returncode, 0)
                self.assertEqual(cli("every", "0.015").returncode, 0)
                self.assertEqual(cli("resume").returncode, 0)
                wait_for(lambda: len(submitted()) >= 2)
                result = cli("stop")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(json.loads(result.stdout)["running"])
                self.assertEqual(len(submitted()), 2)
                self.assertIn("edited while paused", submitted()[1])
            finally:
                cli("stop")


if __name__ == "__main__":
    unittest.main()
