import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
import time
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import browser
import claude_status
import codex_status
import grok_status
import pane_loop as loop
import providers
import usage_pages

NOW = 1789450000.0

# Shapes: Codex wham/usage and Grok credits from omp 18.2.0's parsers; the companion
# organization usage endpoint as documented by public trackers; Qwen from omp's
# Token Plan console parser. Values are synthetic.
CODEX_BODY = {"plan_type": "pro", "rate_limit": {
    "allowed": True, "limit_reached": False,
    "primary_window": {"used_percent": 42, "limit_window_seconds": 18000, "reset_after_seconds": 3600,
                       "reset_at": NOW + 3600},
    "secondary_window": {"used_percent": 18, "limit_window_seconds": 604800, "reset_at": NOW + 86400}},
    "additional_rate_limits": [{"limit_name": "Spark", "rate_limit": {
        "primary_window": {"used_percent": 5, "limit_window_seconds": 18000, "reset_after_seconds": 600}}}]}
COMPANION_BODY = {"five_hour": {"utilization": 17.0, "resets_at": "2026-09-15T01:00:00Z"},
                  "seven_day": {"utilization": 100.0, "resets_at": "2026-09-18T05:00:00Z"},
                  "seven_day_opus": None, "seven_day_sonnet": {"utilization": 3, "resets_at": None},
                  "extra_usage": {"is_enabled": False}}
GROK_BODY = {"data": {"config": {"creditUsagePercent": 64.0, "currentPeriod": {
    "type": "USAGE_PERIOD_TYPE_WEEKLY", "start": "2026-09-09T17:11:41Z", "end": "2026-09-16T17:11:41Z"},
    "productUsage": [{"product": "Build", "usagePercent": 60}]}}}
QWEN_BODY = {"code": "200", "successResponse": True, "data": {"Data": json.dumps({"DataV2": {"data": {"data": {
    "per5HourPercentage": 0.25, "per5HourResetTime": int((NOW + 7200) * 1000),
    "per1WeekPercentage": 64, "per1WeekResetTime": "2026-09-21T16:00:00Z"}}}})}}


def pb_varint(number):
    out = b""
    while True:
        byte, number = number & 0x7F, number >> 7
        out += bytes([byte | 0x80 if number else byte])
        if not number:
            return out


def pb(number, wire, value):
    key = pb_varint(number << 3 | wire)
    if wire == 0:
        return key + pb_varint(value)
    if wire == 5:
        return key + struct.pack("<f", value)
    return key + pb_varint(len(value)) + value


def grok_credits(used, start, end):
    # The GetGrokCreditsConfig layout decoded from grok.com for a signed-in account.
    stamp = lambda seconds: pb(1, 0, int(seconds)) + pb(2, 0, 422174000)
    config = (pb(1, 5, used) + pb(2, 2, b"") + pb(4, 2, stamp(start)) + pb(5, 2, stamp(end))
              + pb(7, 2, pb(1, 0, 2) + pb(2, 5, used))
              + pb(8, 2, pb(1, 0, 2) + pb(2, 2, stamp(start)) + pb(3, 2, stamp(end)))
              + pb(11, 0, 1) + pb(12, 2, pb(1, 0, 103)) + pb(13, 0, 1))
    message, trailer = pb(1, 2, config), b"grpc-status:0\r\n"
    frames = b"\x00" + struct.pack(">I", len(message)) + message + b"\x80" + struct.pack(">I", len(trailer)) + trailer
    return base64.b64encode(frames).decode()


def page(body=None, url="https://example.com/settings/usage", final=None, title="Usage", text="", source=None):
    responses = [] if body is None else [{"url": source or "https://example.com/api/usage", "status": 200, "body": body}]
    return {"url": url, "final_url": final or url, "title": title, "text": text, "responses": responses}


class ExtractorTests(unittest.TestCase):
    def test_codex_windows_with_reset_fallback_and_named_extra_limits(self):
        result = usage_pages.extract(providers.CODEX_ID, page(CODEX_BODY), now=NOW)
        self.assertEqual((result["status"], result["plan"]), ("ok", "pro"))
        windows = {w["name"]: w for w in result["windows"]}
        self.assertEqual(list(windows), ["five_hour", "weekly", "spark_five_hour"])
        self.assertEqual((windows["five_hour"]["used_percent"], windows["five_hour"]["resets_at"]), (42.0, NOW + 3600))
        self.assertEqual(windows["spark_five_hour"]["resets_at"], NOW + 600)
        self.assertEqual(result["evidence"], "example.com/api/usage")

    def test_companion_windows_skip_empty_buckets(self):
        result = usage_pages.extract(providers.COMPANION_ID, page(COMPANION_BODY), now=NOW)
        self.assertEqual([w["name"] for w in result["windows"]], ["five_hour", "weekly", "weekly_sonnet"])
        self.assertEqual(result["windows"][1]["remaining_percent"], 0.0)
        self.assertIsNone(result["windows"][2]["resets_at"])

    def test_grok_period_and_product_usage_are_found_when_nested(self):
        result = usage_pages.extract(providers.GROK_ID, page(GROK_BODY), now=NOW)
        weekly, product = result["windows"]
        self.assertEqual((weekly["name"], weekly["used_percent"], weekly["window_seconds"]), ("weekly", 64.0, 7 * 86400))
        self.assertEqual((product["label"], product["used_percent"]), ("weekly (Build)", 60.0))

    def test_qwen_fractions_milliseconds_and_json_text_fields(self):
        result = usage_pages.extract(providers.QWEN_ID, page(QWEN_BODY), now=NOW)
        windows = {w["name"]: w for w in result["windows"]}
        self.assertEqual((windows["five_hour"]["used_percent"], windows["five_hour"]["resets_at"]), (25.0, NOW + 7200))
        self.assertEqual(windows["weekly"]["used_percent"], 64.0)

    def test_the_newest_matching_response_wins(self):
        older = {"five_hour": {"utilization": 1, "resets_at": None}}
        view = page(older)
        view["responses"].append({"url": "https://example.com/api/usage", "status": 200, "body": COMPANION_BODY})
        self.assertEqual(usage_pages.extract(providers.COMPANION_ID, view)["windows"][0]["used_percent"], 17.0)

    def test_pages_without_data_report_why(self):
        extract = usage_pages.extract
        self.assertEqual(extract(providers.COMPANION_ID, page(title="Just a moment..."))["status"], "challenge")
        self.assertEqual(extract(providers.COMPANION_ID, page(final="https://example.com/login"))["status"],
                         "login_required")
        self.assertEqual(extract(providers.GROK_ID, page(final="https://example.com/"))["status"], "login_required")
        self.assertIn("claudex usage login grok", extract(providers.GROK_ID, page(final="https://example.com/"))["reason"])
        self.assertEqual(extract(providers.QWEN_ID, page({"unrelated": 1}))["status"], "unparsed")
        # Signed-out answers observed on the live pages, which don't redirect.
        self.assertEqual(extract(providers.GROK_ID, page({"status": "unauthenticated"}))["status"], "login_required")
        self.assertEqual(extract(providers.QWEN_ID, page({"code": "ConsoleNeedLogin", "successResponse": False}))["status"],
                         "login_required")
        self.assertEqual(extract(providers.QWEN_ID, page(text="Log in to QwenCloud You are currently not logged in."))["status"],
                         "login_required")


class GrokPageTests(unittest.TestCase):
    CREDITS_URL = "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"

    def test_the_credits_protobuf_gives_the_weekly_period(self):
        view = page(url="https://grok.com/?_s=usage", text="Weekly SuperGrok Plus Limit\nused\n100%\n")
        view["responses"].append({"url": self.CREDITS_URL, "status": 200, "body": None,
                                  "raw": grok_credits(64.25, NOW - 3 * 86400, NOW + 4 * 86400)})
        result = usage_pages.extract(providers.GROK_ID, view, now=NOW)
        weekly = result["windows"][0]
        self.assertEqual((result["status"], result["plan"], result["evidence"]),
                         ("ok", "SuperGrok Plus", "grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"))
        self.assertEqual((weekly["name"], weekly["used_percent"], weekly["window_seconds"]), ("weekly", 64.2, 7 * 86400))
        self.assertAlmostEqual(weekly["resets_at"], NOW + 4 * 86400 + 0.422174, places=3)
        monthly = usage_pages.grok_grpc_windows(grok_credits(10, NOW, NOW + 30 * 86400), NOW)
        self.assertEqual(monthly["windows"][0]["name"], "monthly")
        self.assertIsNone(usage_pages.grok_grpc_windows(base64.b64encode(b"\x00\x00\x00\x00\x02\xff\xff").decode(), NOW))

    def test_the_rendered_view_is_the_fallback(self):
        text = ("Buy Credits\nWeekly SuperGrok Plus Limit\nused\nResets September 16, 2026 at 5:11 PM\n"
                "Grok Build\n100%\nExtra Usage Credits\n")
        result = usage_pages.extract(providers.GROK_ID, page(url="https://grok.com/?_s=usage", text=text), now=NOW)
        weekly = result["windows"][0]
        self.assertEqual((result["status"], result["evidence"], weekly["used_percent"]), ("ok", "page text", 100.0))
        self.assertEqual(weekly["resets_at"], time.mktime(time.strptime("September 16, 2026 5:11 PM", "%B %d, %Y %I:%M %p")))

    def test_debug_outline_shows_protobuf_field_paths(self):
        view = page()
        view["responses"] = [{"url": self.CREDITS_URL, "status": 200, "body": None,
                              "raw": grok_credits(100.0, NOW, NOW + 7 * 86400)}]
        keys = usage_pages.debug_outline(view)["responses"][0]["keys"]
        self.assertEqual((keys["frame0.1.1"], keys["frame0.1.8.1"], keys["frame0.1.12.1"]), (100.0, 2, 103))


class ReportTests(unittest.TestCase):
    def report(self):
        return {"read_at": NOW, "providers": {
            providers.CODEX_ID: {"status": "ok", "checked_at": NOW, **usage_pages.codex_windows(CODEX_BODY, NOW)},
            providers.GROK_ID: {"status": "login_required", "reason": "not signed in", "checked_at": NOW}}}

    def test_table_shows_remaining_unless_used_is_requested(self):
        text = usage_pages.format_report(self.report(), now=NOW)
        self.assertIn("REMAINING", text)
        self.assertIn("58.0% in 1h 0m", text)
        self.assertIn("███████░░░░░", text)
        self.assertNotIn("42.0%", text)
        self.assertIn("login_required", text)
        self.assertNotIn("\x1b[", text)
        self.assertIn("\x1b[32m", usage_pages.format_report(self.report(), now=NOW, color=True))
        used = usage_pages.format_report(self.report(), now=NOW, basis="used")
        self.assertIn("USED", used)
        self.assertIn("42.0% in 1h 0m", used)
        self.assertIn("█████░░░░░░░", used)

    def test_summary_is_one_line_of_5h_and_weekly_remaining(self):
        report = {"providers": {
            providers.CODEX_ID: {"status": "ok", "windows": [
                usage_pages.window("five_hour", 20), usage_pages.window("weekly", 45),
                usage_pages.window("spark_five_hour", 5)]},
            providers.COMPANION_ID: {"status": "ok", "windows": [
                usage_pages.window("five_hour", 100), usage_pages.window("weekly", 68),
                usage_pages.window("weekly_fable", 0)]},
            providers.GROK_ID: {"status": "ok", "windows": [
                usage_pages.window("weekly", 45), usage_pages.window("weekly_build", 60)]},
            providers.QWEN_ID: {"status": "ok", "windows": [
                usage_pages.window("five_hour", 25), usage_pages.window("weekly", 64)]}}}
        self.assertEqual(
            usage_pages.format_summary(report),
            "codex 5h 80 weekly 55; claude 5h 0 weekly 32; grok weekly 55; qwen 5h 75 weekly 36")
        self.assertEqual(
            usage_pages.summary_record(report, basis="used"),
            {"codex": {"5h": 20, "weekly": 45}, "claude": {"5h": 100, "weekly": 68},
             "grok": {"weekly": 45}, "qwen": {"5h": 25, "weekly": 64}})

    def test_the_status_screen_is_printed_only_when_verbose(self):
        report = self.report()
        report["providers"][providers.CODEX_ID]["screen"] = "Weekly limit 55% left\n"
        self.assertNotIn("/status", usage_pages.format_report(report, now=NOW))
        shown = usage_pages.format_report(report, now=NOW, verbose=True)
        self.assertIn("codex /status", shown)
        self.assertIn("Weekly limit 55% left", shown)

    def test_exhaustion_and_cache_merging_keep_earlier_readings_private(self):
        self.assertFalse(usage_pages.exhausted(self.report()))
        full = {"providers": {providers.COMPANION_ID: {"status": "ok", **usage_pages.companion_windows(COMPANION_BODY, NOW)}}}
        self.assertTrue(usage_pages.exhausted(full))
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "usage-pages.json"
            usage_pages.merge_cache(cache, full)
            merged = usage_pages.merge_cache(cache, self.report())
            self.assertEqual(set(merged["providers"]), {providers.CODEX_ID, providers.GROK_ID, providers.COMPANION_ID})
            self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)

    def test_debug_outline_keeps_numbers_and_times_but_not_text(self):
        body = {"account": {"email": "someone@example.com"}, "five_hour": {"utilization": 17.0, "resets_at": "2026-09-15T01:00:00Z"}}
        outline = usage_pages.debug_outline(page(body, source="https://example.com/api/org/123/usage?x=1"))
        keys = outline["responses"][0]["keys"]
        self.assertEqual(keys["account.email"], "<text 19>")
        self.assertEqual((keys["five_hour.utilization"], keys["five_hour.resets_at"]), (17.0, "2026-09-15T01:00:00Z"))
        self.assertEqual(outline["responses"][0]["url"], "example.com/api/org/123/usage")


FAKE_CHROME = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    bodies = {{"r1": {{"five_hour": {{"utilization": 9, "resets_at": None}}}}, "r2": {{"other": 1}}}}
    buffer = b""
    def send(message):
        os.write(4, json.dumps(message).encode() + b"\\0")
    while True:
        chunk = os.read(3, 65536)
        if not chunk:
            break
        buffer += chunk
        while b"\\0" in buffer:
            raw, buffer = buffer.split(b"\\0", 1)
            message = json.loads(raw)
            method, reply = message["method"], {{}}
            if method == "Target.createTarget":
                reply = {{"targetId": "T1"}}
            elif method == "Target.attachToTarget":
                reply = {{"sessionId": "S1"}}
            elif method == "Network.getResponseBody":
                reply = {{"body": json.dumps(bodies[message["params"]["requestId"]]), "base64Encoded": False}}
            elif method == "Runtime.evaluate":
                reply = {{"result": {{"value": json.dumps({{"href": "https://example.com/usage", "title": "Usage", "text": "9% used"}})}}}}
            send({{"id": message["id"], "result": reply}})
            if method == "Page.navigate":
                for rid, url, mime in (("r1", "https://api.example.com/usage", "application/json"),
                                       ("r2", "https://other.org/usage", "application/json"),
                                       ("r3", "https://example.com/app.js", "text/javascript")):
                    send({{"sessionId": "S1", "method": "Network.responseReceived",
                          "params": {{"requestId": rid, "response": {{"url": url, "status": 200, "mimeType": mime}}}}}})
                    send({{"sessionId": "S1", "method": "Network.loadingFinished", "params": {{"requestId": rid}}}})
                send({{"sessionId": "S1", "method": "Page.loadEventFired", "params": {{}}}})
            if method == "Browser.close":
                sys.exit(0)
''')


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.fake = Path(self.folder.name) / "fake-chrome"
        self.fake.write_text(FAKE_CHROME.format(python=sys.executable))
        self.fake.chmod(0o700)

    def tearDown(self):
        self.folder.cleanup()

    def test_capture_keeps_json_from_the_page_domain_and_reads_the_page_text(self):
        profile = Path(self.folder.name) / "profile"
        chrome = browser.ChromePipe(profile, display="headless", binary=str(self.fake))
        try:
            captured = chrome.capture("https://example.com/usage", domains=("example.com",), timeout=5, settle=0.2)
        finally:
            chrome.close()
        self.assertEqual([(r["url"], r["body"]) for r in captured["responses"]],
                         [("https://api.example.com/usage", {"five_hour": {"utilization": 9, "resets_at": None}})])
        self.assertEqual((captured["loaded"], captured["text"]), (True, "9% used"))
        self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o700)

    def test_a_profile_held_by_a_live_chrome_is_busy(self):
        profile = Path(self.folder.name) / "held"
        profile.mkdir()
        os.symlink(f"{os.uname().nodename}-{os.getpid()}", profile / "SingletonLock")
        self.assertEqual(browser.profile_owner(profile), os.getpid())
        with self.assertRaises(browser.ProfileBusy):
            browser.private_profile(profile)
        (profile / "SingletonLock").unlink()
        os.symlink(f"{os.uname().nodename}-999999999", profile / "SingletonLock")
        self.assertIsNone(browser.profile_owner(profile))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.config = Path(self.folder.name) / "loop.json"

    def tearDown(self):
        self.folder.cleanup()

    def run_main(self, *argv):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            status = loop.main(["--config", str(self.config), "usage", *argv])
        return status, output.getvalue()

    def test_read_prints_a_short_line_and_full_json_keeps_the_detail(self):
        report = {"read_at": NOW, "display": "virtual", "providers": {}}
        claude = {"status": "ok", "checked_at": NOW, "screen": "secret screen",
                  "windows": [usage_pages.window("five_hour", 100.0), usage_pages.window("weekly_fable", 0)]}
        grok = {"status": "login_required", "reason": "not signed in", "checked_at": NOW, "windows": []}
        with patch.object(usage_pages, "read_usage", return_value=report) as read, \
                patch.object(claude_status, "read", return_value=claude), \
                patch.object(grok_status, "read", return_value=grok) as grok_read:
            status, output = self.run_main(providers.COMPANION_ID, providers.GROK_ID, "--display", "headless")
        self.assertEqual(status, 1)  # Grok needs manual input from an interactive terminal
        read.assert_not_called()
        grok_read.assert_called_once()
        self.assertEqual(output.strip(), "claude 5h 0; grok input_required")
        status, verbose_line = self.run_main("show", providers.COMPANION_ID, providers.GROK_ID, "--verbose")
        self.assertEqual(verbose_line.strip(), "claude 5h 0; grok input_required")
        cached = json.loads((self.config.parent / ".loop" / "usage-pages.json").read_text())
        self.assertEqual(cached["providers"][providers.COMPANION_ID]["screen"], "secret screen")
        status, output = self.run_main("show", providers.GROK_ID)
        self.assertEqual(status, 1)  # nothing readable among the shown agents
        self.assertEqual(output.strip(), "grok input_required")
        status, output = self.run_main("show", providers.GROK_ID, "--verbose")
        self.assertEqual(output.strip(), "grok input_required")
        status, full = self.run_main("show", providers.COMPANION_ID, providers.GROK_ID, "--full")
        loaded = json.loads(full)
        self.assertEqual(loaded["providers"][providers.GROK_ID]["status"], "input_required")
        self.assertNotIn("screen", loaded["providers"][providers.COMPANION_ID])
        self.assertEqual(loaded["providers"][providers.COMPANION_ID]["windows"][0]["remaining_percent"], 0.0)
        status, human = self.run_main("show", providers.COMPANION_ID, "--human")
        self.assertEqual(status, 2)
        self.assertIn("REMAINING", human)
        self.assertIn("0.0%", human)
        self.assertNotIn("weekly", human)
        self.assertNotIn("secret screen", human)
        status, used = self.run_main("show", providers.COMPANION_ID, "--human", "--used")
        self.assertIn("USED", used)
        self.assertIn("100.0%", used)
        status, verbose = self.run_main("show", providers.COMPANION_ID, "--full", "--verbose")
        self.assertEqual(json.loads(verbose)["providers"][providers.COMPANION_ID]["screen"], "secret screen")
        status, compact = self.run_main("show", providers.COMPANION_ID, "--json")
        self.assertEqual(json.loads(compact), {"claude": {"5h": 0}})

    def test_codex_and_claude_come_from_tmux_and_grok_still_uses_pages(self):
        qwen = {"read_at": NOW, "display": "virtual", "providers": {
            providers.QWEN_ID: {"status": "ok", "checked_at": NOW, "windows": [usage_pages.window("weekly", 64.0)]}}}
        codex = {"status": "ok", "checked_at": NOW, "screen": "Weekly limit 55% left",
                 "windows": [usage_pages.window("weekly", 45.0)]}
        claude = {"status": "ok", "checked_at": NOW, "screen": "Current session",
                  "windows": [usage_pages.window("five_hour", 17.0)]}
        grok = {"status": "ok", "checked_at": NOW, "source": grok_status.SOURCE,
                "windows": [usage_pages.window("weekly", 10.0)]}
        with patch.object(usage_pages, "read_usage", return_value=qwen) as read, \
                patch.object(codex_status, "read", return_value=codex) as codex_read, \
                patch.object(claude_status, "read", return_value=claude) as claude_read, \
                patch.object(grok_status, "read", return_value=grok) as grok_read:
            status, output = self.run_main(providers.GROK_ID, providers.COMPANION_ID, providers.CODEX_ID,
                                           providers.QWEN_ID)
        self.assertEqual(status, 0)
        self.assertEqual(read.call_args.args[0], [providers.QWEN_ID])
        codex_read.assert_called_once()
        claude_read.assert_called_once()
        grok_read.assert_called_once()
        self.assertEqual(output.strip(), "codex weekly 55; claude 5h 83; grok weekly 90; qwen weekly 36")
        status, full = self.run_main("show", "--full")
        loaded = json.loads(full)
        self.assertNotIn("screen", loaded["providers"][providers.CODEX_ID])
        self.assertEqual(loaded["providers"][providers.CODEX_ID]["windows"][0]["remaining_percent"], 55.0)
        with patch.object(usage_pages, "read_usage") as read, \
                patch.object(codex_status, "read", return_value=codex), \
                patch.object(claude_status, "read", return_value=claude):
            self.run_main(providers.CODEX_ID, providers.COMPANION_ID)
        read.assert_not_called()

    def test_login_opens_the_requested_pages_and_bad_agents_are_errors(self):
        with patch.object(browser, "launch_login", return_value=4321) as launch:
            status, output = self.run_main("login", providers.QWEN_ID, "--json")
        self.assertEqual((status, json.loads(output)["pid"]), (0, 4321))
        self.assertEqual(launch.call_args.args[1], [providers.USAGE_PAGES[providers.QWEN_ID]])
        with patch.object(browser, "launch_login") as launch:
            status, output = self.run_main("login", providers.CODEX_ID)
        self.assertEqual(status, 0)
        launch.assert_not_called()
        self.assertIn("codex_usage", output)
        status, output = self.run_main("nonsense")
        self.assertEqual((status, json.loads(output)["status"]), (1, "error"))
        status, output = self.run_main("show")
        self.assertIn("no usage pages have been read yet", output)


if __name__ == "__main__":
    unittest.main()
