from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import route
from providers import CODEX_ID, COMPANION_ID, GROK_ID, QWEN_ID
from usage_pages import window

NOW = 1_800_000_000.0


def entry(windows, status="ok", checked_at=NOW - 60):
    return {"status": status, "checked_at": checked_at, "windows": windows}


def usage(*pairs, checked_at=NOW - 60):
    return {"read_at": checked_at, "providers": {name: entry(windows, checked_at=checked_at)
                                                 for name, windows in pairs}}


class Launch:
    def __init__(self):
        self.calls = []
        self.next_id = 400

    def __call__(self, *args):
        self.calls.append(args)
        self.next_id += 1
        return {"status": "created", "pane": {"pane_id": self.next_id}}


class ChoiceTests(unittest.TestCase):
    def grok_model(self):
        return ("grok-4.7", "xhigh")

    def decide(self, report):
        return route.choose_max(report, now=NOW, grok_model=self.grok_model)

    def test_both_weekly_under_20_uses_grok_xhigh(self):
        report = usage((CODEX_ID, [window("weekly", 85)]),
                       (COMPANION_ID, [window("weekly", 90), window("five_hour", 10)]),
                       (GROK_ID, [window("weekly", 40)]))
        decision = self.decide(report)
        session = decision["sessions"][0]
        self.assertEqual(session["agent"], GROK_ID)
        self.assertEqual(session["argv"], ["grok", "--yolo", "-m", "grok-4.7", "--effort", "xhigh"])
        self.assertEqual(decision["reason"], "both weekly under 20")

    def test_weekly_lead_of_50_picks_the_higher_side(self):
        codex = self.decide(usage((CODEX_ID, [window("weekly", 20)]),
                                  (COMPANION_ID, [window("weekly", 70), window("five_hour", 10)])))
        self.assertEqual(codex["sessions"][0]["argv"][3], "gpt-6-astra")
        self.assertIn("model_reasoning_effort=max", codex["sessions"][0]["argv"])
        claude = self.decide(usage((CODEX_ID, [window("weekly", 80)]),
                                   (COMPANION_ID, [window("weekly", 10), window("five_hour", 90)])))
        self.assertEqual(claude["sessions"][0]["model"], "claude-opus-5-5")
        self.assertEqual(claude["sessions"][0]["effort"], "max")

    def test_claude_5h_over_50_picks_opus_when_the_weekly_gap_is_smaller(self):
        decision = self.decide(usage((CODEX_ID, [window("weekly", 40)]),
                                     (COMPANION_ID, [window("weekly", 50), window("five_hour", 40)])))
        self.assertEqual(decision["reason"], "claude 5h over 50")
        self.assertEqual(decision["sessions"][0]["model"], "claude-opus-5-5")

    def test_5h_of_50_and_a_gap_under_50_defaults_to_astra(self):
        decision = self.decide(usage((CODEX_ID, [window("weekly", 40)]),
                                     (COMPANION_ID, [window("weekly", 55), window("five_hour", 50)])))
        self.assertEqual(decision["reason"], "default astra")
        self.assertEqual(decision["sessions"][0]["model"], "gpt-6-astra")

    def test_missing_and_signed_out_readings_are_not_zero(self):
        decision = self.decide(usage((CODEX_ID, []), (COMPANION_ID, [window("weekly", 95)])))
        self.assertEqual(decision["reason"], "default astra")
        signed_out = {"providers": {
            CODEX_ID: {"status": "login_required", "checked_at": NOW, "windows": []},
            COMPANION_ID: entry([window("weekly", 95), window("five_hour", 80)]),
            QWEN_ID: entry([window("weekly", 100), window("five_hour", 100)]),
        }}
        decision = self.decide(signed_out)
        self.assertEqual(decision["sessions"][0]["agent"], CODEX_ID)
        self.assertNotIn(QWEN_ID, route._numbers(signed_out))

    def test_grok_without_weekly_remaining_does_not_win_the_low_week(self):
        report = usage((CODEX_ID, [window("weekly", 90)]),
                       (COMPANION_ID, [window("weekly", 90), window("five_hour", 60)]),
                       (GROK_ID, [window("weekly", 100)]))
        decision = self.decide(report)
        self.assertEqual(decision["reason"], "default astra")
        self.assertEqual(decision["skipped"], ["grok out"])

    def test_security_chain_stops_at_the_first_agent_with_tokens(self):
        grok = route.choose_security(usage((GROK_ID, [window("weekly", 40)])), now=NOW)
        self.assertEqual(grok["sessions"][0]["argv"], ["grok", "--yolo"])
        claude = route.choose_security(usage((GROK_ID, [window("weekly", 100)]),
                                             (COMPANION_ID, [window("five_hour", 10), window("weekly", 40)])), now=NOW)
        self.assertEqual(claude["sessions"][0]["model"], "claude-opus-5-5")
        self.assertEqual(claude["sessions"][0]["effort"], "high")
        codex = route.choose_security(usage((GROK_ID, [window("weekly", 100)]),
                                            (COMPANION_ID, [window("five_hour", 100), window("weekly", 40)])), now=NOW)
        self.assertEqual(codex["sessions"][0]["argv"],
                         ["codex", "--yolo", "-m", "gpt-6-astra", "-c", "model_reasoning_effort=high"])

    def test_a_reset_window_is_not_still_out_of_tokens(self):
        report = usage((GROK_ID, [window("weekly", 100, NOW - 5)]))
        decision = route.choose_security(report, now=NOW)
        self.assertEqual(decision["sessions"][0]["agent"], GROK_ID)

    def test_signed_out_grok_moves_to_claude(self):
        report = {"providers": {GROK_ID: {"status": "login_required", "checked_at": NOW, "windows": []},
                                COMPANION_ID: entry([window("weekly", 10)])}}
        decision = route.choose_security(report, now=NOW)
        self.assertEqual(decision["sessions"][0]["agent"], COMPANION_ID)
        self.assertEqual(decision["skipped"], ["grok login_required"])

    def test_worker_tiers_launch_both_sides(self):
        low = route.choose_worker("low")
        self.assertEqual([(s["model"], s["effort"]) for s in low["sessions"]],
                         [("gpt-6-luna", "max"), ("claude-sonnet-5-5", "low")])
        mid = route.choose_worker("mid")
        self.assertEqual([(s["model"], s["effort"]) for s in mid["sessions"]],
                         [("gpt-6.1-terra", "high"), ("claude-sonnet-5-5", "medium")])
        high = route.choose_worker("high")
        self.assertEqual([(s["model"], s["effort"]) for s in high["sessions"]],
                         [("gpt-6.1-sol", "high"), ("claude-sonnet-5-5", "high")])
        top = route.choose_worker("max")
        self.assertEqual([(s["model"], s["effort"]) for s in top["sessions"]],
                         [("gpt-6.1-sol", "max"), ("claude-sonnet-5-5", "max")])
        self.assertIn("--dangerously-skip-permissions", top["sessions"][1]["argv"])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.cache = Path(self.folder.name) / "usage-pages.json"
        self.calls = []

    def tearDown(self):
        self.folder.cleanup()

    def write(self, report):
        self.cache.write_text(json.dumps(report), encoding="utf-8")

    def reader(self, name, windows):
        def read():
            self.calls.append(name)
            return entry(windows, checked_at=NOW)
        return read

    def readers(self):
        return {CODEX_ID: self.reader(CODEX_ID, [window("weekly", 20), window("five_hour", 20)]),
                COMPANION_ID: self.reader(COMPANION_ID, [window("weekly", 30), window("five_hour", 80)]),
                GROK_ID: self.reader(GROK_ID, [window("weekly", 40)])}

    def test_a_reading_younger_than_30_minutes_is_not_reread(self):
        self.write(usage((CODEX_ID, [window("weekly", 20)]),
                         (COMPANION_ID, [window("weekly", 30), window("five_hour", 80)]),
                         checked_at=NOW - (route.USAGE_TTL - 1)))
        route.ensure_usage([CODEX_ID, COMPANION_ID], self.cache, now=NOW, readers=self.readers())
        self.assertEqual(self.calls, [])

    def test_a_reading_30_minutes_old_is_reread_and_then_kept(self):
        self.write(usage((CODEX_ID, [window("weekly", 20)]),
                         (COMPANION_ID, [window("weekly", 30)]),
                         checked_at=NOW - route.USAGE_TTL))
        route.ensure_usage([CODEX_ID, COMPANION_ID], self.cache, now=NOW, readers=self.readers())
        self.assertEqual(self.calls, [CODEX_ID, COMPANION_ID])
        self.calls.clear()
        route.ensure_usage([CODEX_ID, COMPANION_ID, GROK_ID], self.cache, now=NOW + 10,
                           readers=self.readers())
        self.assertEqual(self.calls, [GROK_ID])

    def test_the_report_timestamp_does_not_keep_a_stale_provider(self):
        self.write({"read_at": NOW, "providers": {
            CODEX_ID: entry([window("weekly", 20)], checked_at=NOW - route.USAGE_TTL),
            COMPANION_ID: entry([window("weekly", 30)], checked_at=NOW - 10),
        }})
        route.ensure_usage([CODEX_ID, COMPANION_ID], self.cache, now=NOW, readers=self.readers())
        self.assertEqual(self.calls, [CODEX_ID])

    def test_max_does_not_read_grok_or_qwen_unless_both_weeks_are_under_20(self):
        self.write(usage((CODEX_ID, [window("weekly", 20)]),
                         (COMPANION_ID, [window("weekly", 30), window("five_hour", 80)]),
                         (QWEN_ID, [window("weekly", 100)]),
                         checked_at=NOW - route.USAGE_TTL))
        launch = Launch()
        decision = route.run("max", cache=self.cache, cwd=Path(self.folder.name), now=NOW,
                             readers=self.readers(), launch=launch, dry_run=True)
        self.assertEqual(self.calls, [CODEX_ID, COMPANION_ID])
        self.assertEqual(decision["sessions"][0]["agent"], CODEX_ID)
        self.assertEqual(launch.calls, [])

    def test_max_reads_only_a_stale_grok_when_both_weeks_are_under_20(self):
        self.write(usage((CODEX_ID, [window("weekly", 90)]),
                         (COMPANION_ID, [window("weekly", 85), window("five_hour", 10)]),
                         checked_at=NOW - 30))
        launch = Launch()
        decision = route.run("max", cache=self.cache, cwd=Path(self.folder.name), now=NOW,
                             readers=self.readers(), launch=launch, grok_home=Path(self.folder.name))
        self.assertEqual(self.calls, [GROK_ID])
        self.assertEqual(decision["sessions"][0]["effort"], "xhigh")
        self.assertEqual(decision["refreshed"], [GROK_ID])
        self.assertEqual(decision["window_ids"], [401])
        self.assertEqual(launch.calls[0][:4], ("grid", "add", "--name", "claudex-route"))
        self.assertIn("--new", launch.calls[0])
        self.assertEqual(launch.calls[0][launch.calls[0].index("--title") + 1], "claudex-route-max")
        self.assertNotIn("--no-hold", launch.calls[0])

    def test_security_does_not_read_claude_when_grok_has_tokens(self):
        self.write(usage((GROK_ID, [window("weekly", 40)]), checked_at=NOW - 60))
        decision = route.run("security", cache=self.cache, cwd=Path(self.folder.name), now=NOW,
                             readers=self.readers(), launch=Launch(), dry_run=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(decision["sessions"][0]["agent"], GROK_ID)

    def test_worker_does_not_read_usage_and_opens_both_panes(self):
        launch = Launch()
        decision = route.run("worker", "low", cache=self.cache, cwd=Path(self.folder.name), now=NOW,
                             readers=self.readers(), launch=launch)
        self.assertEqual(self.calls, [])
        self.assertEqual(decision["window_ids"], [401, 402])
        self.assertEqual(launch.calls[0][:4], ("grid", "add", "--name", "claudex-route"))
        self.assertIn("--new", launch.calls[0])
        self.assertIn("--into", launch.calls[1])
        self.assertEqual(launch.calls[1][launch.calls[1].index("--into") + 1], "401")
        self.assertNotIn("--location=vsplit", launch.calls[1])
        self.assertIn("--", launch.calls[1])
        self.assertTrue(route.format_line(decision).startswith(
            "opened worker low codex gpt-6-luna max; claude claude-sonnet-5-5 low"))

    def test_dry_run_prints_the_choice_without_launching(self):
        self.write(usage((CODEX_ID, [window("weekly", 10)]),
                         (COMPANION_ID, [window("weekly", 70), window("five_hour", 10)]),
                         checked_at=NOW - 10))
        launch = Launch()
        decision = route.run("max", cache=self.cache, cwd=Path(self.folder.name), now=NOW,
                             readers=self.readers(), launch=launch, dry_run=True)
        self.assertEqual(launch.calls, [])
        self.assertEqual(route.format_line(decision), "would open max codex gpt-6-astra max")
        record = route.public_record(decision)
        self.assertNotIn("screen", json.dumps(record))
        self.assertEqual(record["reason"], "weekly lead 60")
        self.assertEqual(record["refreshed"], [])
        self.assertEqual(set(record["usage"]), {CODEX_ID, COMPANION_ID})


class GrokMenuTests(unittest.TestCase):
    def test_missing_menu_uses_xhigh(self):
        self.assertEqual(route.grok_target(home=Path("/no/such-grok-home")), ("grok-4.7", "xhigh"))

    def test_max_on_the_menu_is_used(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        home = Path(folder.name)
        (home / "models_cache.json").write_text(json.dumps({"models": {"grok-9": {"info": {
            "model": "grok-9", "description": "SpaceXAI's latest frontier model",
            "reasoning_efforts": [{"id": "max"}, {"id": "xhigh"}, {"id": "high"}]}}}}), encoding="utf-8")
        self.assertEqual(route.grok_target(home=home), ("grok-9", "max"))


class RouteBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.cache = Path(self.folder.name) / "usage-pages.json"
        self.calls = []

    def tearDown(self):
        self.folder.cleanup()

    def readers(self):
        def read(name):
            def inner():
                self.calls.append(name)
                return entry([])
            return inner
        return {CODEX_ID: read(CODEX_ID), COMPANION_ID: read(COMPANION_ID), GROK_ID: read(GROK_ID)}

    def test_kilix_tools_are_rejected_before_usage_or_launch(self):
        for name in ("grammar", "needle", "shell"):
            with self.subTest(name=name):
                launch = Launch()
                with self.assertRaises(route.RouteError):
                    route.run(name, cache=self.cache, cwd=Path(self.folder.name), now=NOW,
                              readers=self.readers(), launch=launch)
                self.assertEqual(self.calls, [])
                self.assertEqual(launch.calls, [])
                self.assertFalse(self.cache.exists())

    def test_verb_is_not_a_route(self):
        self.assertNotIn("verb", route.ROUTE_NAMES)
        with self.assertRaises(route.RouteError):
            route.run("verb", cache=self.cache, cwd=Path(self.folder.name), now=NOW,
                      readers=self.readers(), dry_run=True)



class RequestTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.cache = Path(self.folder.name) / "usage-pages.json"
        self.root = Path(self.folder.name)
        self.note = self.root / "note.py"
        self.note.write_text("x\n", encoding="utf-8")
        self.calls = []

    def tearDown(self):
        self.folder.cleanup()

    def readers(self):
        def read(name):
            def inner():
                self.calls.append(name)
                return entry([])
            return inner
        return {CODEX_ID: read(CODEX_ID), COMPANION_ID: read(COMPANION_ID), GROK_ID: read(GROK_ID)}

    def test_a_request_parses_when_every_path_is_a_file(self):
        other = self.root / "docs" / "plan.md"
        other.parent.mkdir()
        other.write_text("y\n", encoding="utf-8")
        spaced = self.root / "my file.py"
        spaced.write_text("z\n", encoding="utf-8")
        text = f"fix {self.note} and '{other}' and \"{spaced}\" and skip https://example.com/a"
        found = route.check_request(text, self.root)
        self.assertTrue(found["ok"])
        self.assertEqual(found["files"], [str(self.note.resolve()), str(other.resolve()), str(spaced.resolve())])
        relative = route.check_request("fix ./note.py", self.root)
        self.assertEqual(relative["files"], [str(self.note.resolve())])

    def test_a_missing_path_or_a_directory_does_not_parse(self):
        missing = route.check_request("fix /no/such/claudex-route.py", self.root)
        self.assertFalse(missing["ok"])
        self.assertIn("missing", missing["reason"])
        self.assertIn("/no/such/claudex-route.py", missing["reason"])
        folder = route.check_request(f"fix {self.root}", self.root)
        self.assertFalse(folder["ok"])
        self.assertIn("not a file", folder["reason"])
        empty = route.check_request("prompt including no path", self.root)
        self.assertEqual(empty["reason"], "no file path")

    def test_dry_run_names_the_model_and_a_parsed_request(self):
        launch = Launch()
        prompt = f"fix {self.note}"
        decision = route.run("worker", "low", cache=self.cache, cwd=self.root, now=NOW,
                             readers=self.readers(), launch=launch, dry_run=True, prompt=prompt)
        self.assertEqual(launch.calls, [])
        self.assertEqual(self.calls, [])
        line = route.format_line(decision)
        self.assertTrue(line.startswith(
            "would open worker low codex gpt-6-luna max; claude claude-sonnet-5-5 low"))
        self.assertIn(f"parse ok {self.note.resolve()}", line)
        self.assertEqual(decision["sessions"][0]["argv"][-1], prompt)
        self.assertEqual(decision["sessions"][1]["argv"][-1], prompt)
        record = route.public_record(decision)
        self.assertTrue(record["request"]["ok"])
        self.assertEqual(record["request"]["files"], [str(self.note.resolve())])

    def test_a_bad_request_does_not_open_a_pane(self):
        launch = Launch()
        decision = route.run("worker", "low", cache=self.cache, cwd=self.root, now=NOW,
                             readers=self.readers(), launch=launch, prompt="just do it")
        self.assertEqual(launch.calls, [])
        line = route.format_line(decision)
        self.assertIn("would open worker low codex gpt-6-luna max", line)
        self.assertIn("parse fail no file path", line)
        self.assertFalse(route.public_record(decision)["request"]["ok"])

    def test_the_cli_dry_run_reports_the_model_and_the_parse(self):
        from pane_loop import route_command
        from argparse import Namespace

        out = StringIO()
        with patch("sys.stdout", out):
            code = route_command(Namespace(
                name="worker", request=["low", f"fix {self.note}"], dry_run=True, json=False,
                cwd=self.root), Path("."))
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("gpt-6-luna", text)
        self.assertIn("claude-sonnet-5-5", text)
        self.assertIn("parse ok", text)
        out = StringIO()
        with patch("sys.stdout", out):
            code = route_command(Namespace(
                name="worker", request=["high", "fix /no/such/claudex-route.py"], dry_run=True, json=True,
                cwd=self.root), Path("."))
        self.assertEqual(code, 1)
        record = json.loads(out.getvalue())
        self.assertFalse(record["request"]["ok"])
        self.assertIn("missing", record["request"]["reason"])
        self.assertEqual(record["sessions"][0]["model"], "gpt-6.1-sol")
        self.assertEqual(record["sessions"][0]["effort"], "high")
        self.assertEqual(record["sessions"][1]["model"], "claude-sonnet-5-5")


class CommandTests(unittest.TestCase):
    def test_the_cli_rejects_a_worker_without_a_tier(self):
        from pane_loop import LoopError, route_command
        from argparse import Namespace

        with self.assertRaises(LoopError):
            route_command(Namespace(name="worker", tier=None, dry_run=True, json=False, cwd=None), Path("."))

    def test_the_cli_rejects_a_tier_on_security(self):
        from pane_loop import LoopError, route_command
        from argparse import Namespace

        with self.assertRaises(LoopError):
            route_command(Namespace(name="security", tier="low", dry_run=True, json=False, cwd=None), Path("."))
