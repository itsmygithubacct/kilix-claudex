from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

import limits
import providers

# Limit detection reads account-wide files; never let tests see real ones.
for variable in (providers.CODEX_HOME_ENV, providers.COMPANION_CONFIG_ENV, providers.GROK_HOME_ENV,
                 providers.OMP_AGENT_DIR_ENV):
    os.environ[variable] = tempfile.mkdtemp(prefix="claudex-test-")

NOW = 1789450000.0


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def window(used, minutes, resets):
    return {"used_percent": used, "window_minutes": minutes, "resets_at": resets}


def codex_event(epoch, limit_id="codex", primary=None, secondary=None, reached=None):
    # Shape copied from a codex-cli 0.154.0 rollout token_count event.
    return {"timestamp": iso(epoch), "type": "event_msg",
            "payload": {"type": "token_count", "info": {},
                        "rate_limits": {"limit_id": limit_id, "limit_name": None, "primary": primary,
                                        "secondary": secondary,
                                        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                                        "individual_limit": None, "spend_control_reached": None,
                                        "plan_type": "pro", "rate_limit_reached_type": reached}}}


def companion_rejection(epoch, kind, resets):
    # Shape of a companion agent transcript record for a rejected (429) request.
    return {"type": "assistant", "timestamp": iso(epoch), "error": "rate_limit", "isApiErrorMessage": True,
            "apiErrorStatus": 429,
            "message": {"content": [{"type": "text", "text": "limit"}]},
            "quotaLimits": {"status": "rejected", "resetsAt": resets, "rateLimitType": kind,
                            "overageStatus": "rejected", "isUsingOverage": False}}


def write_lines(path, records, filler=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for record in records:
            for _ in range(filler):
                stream.write(json.dumps({"type": "response_item", "payload": {"text": "x" * 200}}) + "\n")
            stream.write(json.dumps(record) + "\n")


def codex_pane(path=None):
    return {"pane_id": 1, "coding_session": {"provider": "codex", "session_id": "s", "path": path}}


def companion_pane(session_id="11111111-2222-3333-4444-555555555555"):
    return {"pane_id": 2, "coding_session": {"provider": providers.COMPANION_ID, "session_id": session_id, "path": None}}


def grok_billing(epoch, used, start, end, kind="USAGE_PERIOD_TYPE_WEEKLY", prepaid=0):
    # Shape of a grok 1.0.25 unified.jsonl billing record, account details omitted.
    return {"ts": iso(epoch), "src": "shell", "pid": 4242, "ver": "1.0.25", "lvl": "info",
            "msg": "billing: fetched credits config",
            "ctx": {"config": {"creditUsagePercent": used,
                               "currentPeriod": {"type": kind, "start": iso(start), "end": iso(end)},
                               "onDemandCap": {"val": 0}, "onDemandUsed": {"val": 0},
                               "prepaidBalance": {"val": prepaid}, "isUnifiedBillingUser": True,
                               "billingPeriodStart": iso(start), "billingPeriodEnd": iso(end), "historyLen": 0},
                    "onDemandEnabled": False, "subscriptionTier": "SuperGrok"}}


def grok_pane(cwd="/srv/work"):
    return {"pane_id": 3, "cwd": cwd, "coding_session": None,
            "process": {"name": "grok", "argv": ["grok"],
                        "foreground": [{"pid": 7, "argv": ["/opt/grok/bin/grok", "--continue"], "cwd": cwd}]}}


def omp_pane(model="qwen-token-plan/qwen3.8-max", cwd="/srv/work"):
    argv = ["omp", "--model", model] if model else ["omp"]
    return {"pane_id": 4, "cwd": cwd, "coding_session": None,
            "process": {"name": "omp", "argv": argv, "foreground": [{"pid": 8, "argv": argv, "cwd": cwd}]}}


def omp_session(cwd, model="qwen-token-plan/qwen3.8-max", epoch=NOW - 7200):
    # Header records from an omp 18.2.0 session file.
    return [{"type": "title", "v": 1, "title": "", "source": "", "updatedAt": iso(epoch)},
            {"type": "session", "version": 3, "id": "01a0a642", "timestamp": iso(epoch), "cwd": cwd},
            {"type": "model_change", "id": "bd5e5080", "parentId": None, "timestamp": iso(epoch), "model": model}]


def omp_reply(epoch, error=None, provider="qwen-token-plan"):
    # Assistant record shape from an omp 18.2.0 session. A failed request carries stopReason
    # "error" and errorMessage; the 429 text is Alibaba's documented Token Plan rejection.
    message = {"role": "assistant", "api": "openai-completions", "provider": provider, "model": "qwen3.8-max",
               "stopReason": "error" if error else "stop", "timestamp": int(epoch * 1000)}
    if error:
        message["errorMessage"] = error
    return {"type": "message", "id": f"r{int(epoch)}", "timestamp": iso(epoch), "message": message}


class TailTests(unittest.TestCase):
    def test_lines_come_newest_first_across_chunk_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "x.jsonl"
            lines = [f'{{"n": {i}, "tag": "{"hit" if i % 3 == 0 else "miss"}"}}' for i in range(60)]
            path.write_text("\n".join(lines) + "\n")
            found = [json.loads(line)["n"] for line in limits.tail_lines(path, b'"hit"', chunk=7)]
            self.assertEqual(found, [i for i in reversed(range(60)) if i % 3 == 0])
            self.assertEqual(len(list(limits.tail_lines(path, b'"hit"', chunk=7, limit=3))), 3)
            self.assertEqual(list(limits.tail_lines(Path(folder) / "missing", b"x")), [])


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.home = Path(self.folder.name) / "codex"
        self.day = self.home / "sessions" / "2026" / "09" / "15"

    def tearDown(self):
        self.folder.cleanup()

    def assess(self, pane, **kwargs):
        return limits.assess_limits(pane, now=NOW, codex_home=self.home,
                                    companion_home=Path(self.folder.name) / "none", **kwargs)

    def test_weekly_only_bucket_reports_weekly_and_marks_five_hour_not_reported(self):
        path = self.day / "rollout-a.jsonl"
        write_lines(path, [codex_event(NOW - 900, primary=window(90.0, 10080, NOW + 86400)),
                           codex_event(NOW - 60, primary=window(99.0, 10080, NOW + 90000))], filler=3)
        result = self.assess(codex_pane(str(path)), low_percent=0)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["windows"]["weekly"]["remaining_percent"], 1.0)
        self.assertEqual(result["not_reported"], ["five_hour"])
        self.assertIn("weekly 99% used (1% left)", result["reason"])
        self.assertEqual(self.assess(codex_pane(str(path)), low_percent=10)["state"], "low")

    def test_five_hour_window_is_reported_where_the_bucket_has_one(self):
        path = self.day / "rollout-b.jsonl"
        write_lines(path, [codex_event(NOW - 30, limit_id="codex_bengalfox",
                                       primary=window(100.0, 300, NOW + 1200),
                                       secondary=window(40.0, 10080, NOW + 400000))])
        result = self.assess(codex_pane(str(path)))
        self.assertEqual(result["state"], "exhausted")
        self.assertEqual(result["exhausted"], ["five_hour"])
        self.assertEqual(result["windows"]["weekly"]["used_percent"], 40.0)
        self.assertEqual(result["not_reported"], [])

    def test_a_window_that_has_reset_since_it_was_observed_does_not_block(self):
        path = self.day / "rollout-c.jsonl"
        write_lines(path, [codex_event(NOW - 7200, primary=window(100.0, 300, NOW - 60))])
        result = self.assess(codex_pane(str(path)))
        self.assertNotEqual(result["state"], "exhausted")
        self.assertTrue(result["windows"]["five_hour"]["reset_since_observed"])

    def test_limits_are_account_wide_so_a_newer_rollout_wins(self):
        own = self.day / "rollout-own.jsonl"
        other = self.day / "rollout-other.jsonl"
        write_lines(own, [codex_event(NOW - 3600, primary=window(50.0, 10080, NOW + 86400))])
        write_lines(other, [codex_event(NOW - 10, primary=window(100.0, 10080, NOW + 86400))])
        os.utime(own, (NOW - 3600, NOW - 3600))
        result = self.assess(codex_pane(str(own)))
        self.assertEqual(result["state"], "exhausted")
        self.assertEqual(result["source_file"], str(other))

    def test_a_bucket_without_windows_falls_back_to_the_newest_bucket_with_windows(self):
        path = self.day / "rollout-d.jsonl"
        write_lines(path, [codex_event(NOW - 600, primary=window(97.0, 10080, NOW + 86400)),
                           codex_event(NOW - 5, limit_id="premium")])
        result = self.assess(codex_pane(str(path)))
        self.assertEqual(result["limit_id"], "codex")
        self.assertIn("premium reports no windows", result["note"])
        self.assertEqual(result["windows"]["weekly"]["used_percent"], 97.0)

    def test_a_reached_type_means_exhausted(self):
        path = self.day / "rollout-e.jsonl"
        write_lines(path, [codex_event(NOW - 5, primary=window(80.0, 10080, NOW + 86400), reached="weekly")])
        self.assertEqual(self.assess(codex_pane(str(path)))["state"], "exhausted")

    def test_no_evidence_is_unknown_and_never_exhausted(self):
        result = self.assess(codex_pane(None))
        self.assertEqual((result["state"], result["reason"]), ("unknown", "no limit evidence found"))
        self.assertEqual(limits.assess_limits({"coding_session": None}, now=NOW)["state"], "unknown")


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.home = Path(self.folder.name) / "companion"

    def tearDown(self):
        self.folder.cleanup()

    def assess(self, pane, **kwargs):
        return limits.assess_limits(pane, now=NOW, companion_home=self.home,
                                    codex_home=Path(self.folder.name) / "none", **kwargs)

    def test_a_current_rejection_marks_its_window_exhausted(self):
        pane = companion_pane()
        write_lines(self.home / "projects" / "-srv-work" / f"{pane['coding_session']['session_id']}.jsonl",
                    [companion_rejection(NOW - 120, "five_hour", NOW + 3600)], filler=2)
        result = self.assess(pane)
        self.assertEqual(result["state"], "exhausted")
        self.assertEqual(result["windows"]["five_hour"]["status"], "rejected")
        self.assertEqual(result["not_reported"], ["weekly"])
        self.assertIn("usage percentages are not available", result["note"])

    def test_seven_day_maps_to_weekly_and_an_expired_rejection_is_unknown(self):
        write_lines(self.home / "projects" / "-srv-other" / "22222222-aaaa-bbbb-cccc-dddddddddddd.jsonl",
                    [companion_rejection(NOW - 90000, "seven_day", NOW - 60)])
        result = self.assess(companion_pane())
        self.assertIn("weekly", result["windows"])
        self.assertEqual(result["state"], "unknown")

    def test_model_specific_weekly_limits_and_warning_utilization(self):
        # Shapes from strings in the companion agent's 2.1.272 bundle: seven_day_sonnet, and
        # allowed_warning records carrying utilization.
        warning = companion_rejection(NOW - 30, "seven_day", NOW + 86400)
        warning["quotaLimits"].update(status="allowed_warning", utilization=0.93)
        write_lines(self.home / "projects" / "-srv-x" / "33333333-aaaa-bbbb-cccc-dddddddddddd.jsonl",
                    [companion_rejection(NOW - 60, "seven_day_sonnet", NOW + 7200), warning])
        result = self.assess(companion_pane(), low_percent=10)
        self.assertEqual(result["windows"]["weekly_sonnet"]["limit_type"], "seven_day_sonnet")
        self.assertEqual(result["windows"]["weekly"]["used_percent"], 93.0)
        self.assertEqual(result["state"], "exhausted")  # the sonnet weekly limit is rejected
        self.assertIn("weekly 93% used (7% left)", result["reason"])

    def test_no_rejections_is_unknown(self):
        self.assertEqual(self.assess(companion_pane())["state"], "unknown")


class GrokTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.home = Path(self.folder.name) / "grok"
        self.log = self.home / "logs" / "unified.jsonl"

    def tearDown(self):
        self.folder.cleanup()

    def assess(self, pane, text=None, **kwargs):
        return limits.assess_limits(pane, text, now=NOW, grok_home=self.home,
                                    omp_home=Path(self.folder.name) / "omp", **kwargs)

    def test_a_grok_pane_is_detected_from_its_command_and_reports_the_weekly_period(self):
        start, end = NOW - 3 * 86400, NOW + 4 * 86400
        write_lines(self.log, [grok_billing(NOW - 600, 62.5, start, end), grok_billing(NOW - 30, 64.0, start, end)],
                    filler=2)
        result = self.assess(grok_pane())
        self.assertEqual((result["agent"], result["detected_from"], result["state"]), ("grok", "process", "ok"))
        weekly = result["windows"]["weekly"]
        self.assertEqual((weekly["used_percent"], weekly["window_minutes"], weekly["resets_in_seconds"]),
                         (64.0, 10080, 4 * 86400))
        self.assertEqual((result["not_reported"], result["plan_type"]), (["five_hour"], "SuperGrok"))

    def test_a_used_allowance_is_exhausted_until_its_period_ends(self):
        write_lines(self.log, [grok_billing(NOW - 60, 100.0, NOW - 86400, NOW + 6 * 86400, prepaid=212)])
        result = self.assess(grok_pane())
        self.assertEqual(result["state"], "exhausted")
        self.assertIn("prepaid balance 212", result["note"])
        write_lines(self.log, [grok_billing(NOW - 8 * 86400, 100.0, NOW - 14 * 86400, NOW - 7 * 86400)])
        self.assertEqual(self.assess(grok_pane())["state"], "unknown")

    def test_monthly_periods_keep_their_name(self):
        write_lines(self.log, [grok_billing(NOW - 60, 20.0, NOW - 10 * 86400, NOW + 20 * 86400,
                                            kind="USAGE_PERIOD_TYPE_MONTHLY")])
        result = self.assess(grok_pane())
        self.assertEqual((list(result["windows"]), result["not_reported"]), (["monthly"], ["weekly", "five_hour"]))

    def test_limit_report_adds_the_current_period_history_and_burn_rate(self):
        start, end = NOW - 86400, NOW + 6 * 86400
        write_lines(self.log, [grok_billing(NOW - 5 * 86400, 90.0, NOW - 8 * 86400, NOW - 86400)]
                    + [grok_billing(NOW - 3600 + i * 600, 40.0 + i, start, end) for i in range(7)])
        report = limits.limit_report(grok_pane(), now=NOW, grok_home=self.home,
                                     omp_home=Path(self.folder.name) / "omp")
        weekly = report["windows"]["weekly"]
        self.assertEqual([used for _, used in weekly["history"]], [40.0 + i for i in range(7)])
        self.assertAlmostEqual(weekly["burn_percent_per_hour"], 6.0, places=1)


class QwenTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.home = Path(self.folder.name) / "omp"

    def tearDown(self):
        self.folder.cleanup()

    def assess(self, pane, text=None, **kwargs):
        return limits.assess_limits(pane, text, now=NOW, omp_home=self.home,
                                    grok_home=Path(self.folder.name) / "grok", **kwargs)

    def write(self, folder, cwd, replies, model="qwen-token-plan/qwen3.8-max"):
        write_lines(self.home / "sessions" / folder / "2026-09-15T00-00-00-000Z_01a0a642.jsonl",
                    omp_session(cwd, model) + replies)

    def test_a_seven_day_rejection_is_exhausted_until_a_later_request_succeeds(self):
        rejection = "429 Allocated quota exceeded: 7-day limit exhausted"
        self.write("-srv-work", "/srv/work", [omp_reply(NOW - 900), omp_reply(NOW - 300, error=rejection)])
        result = self.assess(omp_pane())
        self.assertEqual((result["agent"], result["state"]), ("qwen", "exhausted"))
        self.assertEqual((result["windows"]["weekly"]["status"], result["model"]),
                         ("rejected", "qwen-token-plan/qwen3.8-max"))
        self.assertEqual(result["events"][0]["limit_type"], "weekly")
        self.write("-srv-work", "/srv/work", [omp_reply(NOW - 300, error=rejection), omp_reply(NOW - 60)])
        result = self.assess(omp_pane())
        self.assertEqual(result["state"], "unknown")
        self.assertIn("a later request succeeded", result["reason"])

    def test_the_model_comes_from_the_session_when_the_command_names_none(self):
        self.write("-srv-app", "/srv/app", [omp_reply(NOW - 120)])
        result = self.assess(omp_pane(model=None, cwd="/srv/app"))
        self.assertEqual((result["agent"], result["state"]), ("qwen", "unknown"))
        self.assertIn("last successful request 2m ago", result["reason"])
        self.assertEqual(self.assess(omp_pane(model="ollama/qwen3.8:27b"))["agent"], "")

    def test_rejections_are_account_wide_and_other_errors_are_ignored(self):
        self.write("-srv-other", "/srv/other", [
            omp_reply(NOW - 400, error="429 Allocated quota exceeded: 5-hour limit exhausted"),
            omp_reply(NOW - 200, error="400 Range of input length error-code/#token-limit you exceeded your current quota"),
            omp_reply(NOW - 100, error="Provider finish_reason: network_error"),
            omp_reply(NOW - 50, error="429 quota exceeded", provider="ollama")])
        result = self.assess(omp_pane())
        self.assertEqual((list(result["windows"]), result["state"]), (["five_hour"], "exhausted"))

    def test_a_rejection_older_than_its_window_does_not_count(self):
        self.write("-srv-work", "/srv/work", [omp_reply(NOW - 6 * 3600, error="429 Allocated quota exceeded: 5 hour limit")])
        self.assertEqual(self.assess(omp_pane())["state"], "unknown")


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.home = Path(self.folder.name) / "codex"
        self.path = self.home / "sessions" / "2026" / "09" / "15" / "rollout-h.jsonl"

    def tearDown(self):
        self.folder.cleanup()

    def append(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(text)

    def test_reads_incrementally_and_waits_for_complete_lines(self):
        resets = NOW + 86400
        self.append(json.dumps(codex_event(NOW - 7200, primary=window(10.0, 10080, resets))) + "\n")
        self.append(json.dumps(codex_event(NOW - 3600, primary=window(12.0, 10080, resets))) + "\n")
        history = limits.CodexHistory(self.home, chunk=64).refresh(NOW)
        self.assertEqual([u for _, u in history.series("codex", "weekly", resets, 10080)], [10.0, 12.0])
        partial = json.dumps(codex_event(NOW - 60, primary=window(14.0, 10080, resets + 2)))
        self.append(partial[:40])
        history.refresh(NOW)
        self.assertEqual(len(history.series("codex", "weekly", resets, 10080)), 2)
        self.append(partial[40:] + "\n")
        history.refresh(NOW)
        self.assertEqual([u for _, u in history.series("codex", "weekly", resets, 10080)], [10.0, 12.0, 14.0])

    def test_series_keeps_only_the_current_window(self):
        resets = NOW + 3600
        self.append("\n".join(json.dumps(e) for e in (
            codex_event(NOW - 7 * 86400 - 60, primary=window(90.0, 10080, resets - 7 * 86400)),
            codex_event(NOW - 600, primary=window(5.0, 10080, resets - 1)),
            codex_event(NOW - 300, primary=window(6.0, 10080, resets + 1)))) + "\n")
        history = limits.CodexHistory(self.home).refresh(NOW)
        self.assertEqual([u for _, u in history.series("codex", "weekly", resets, 10080)], [5.0, 6.0])

    def test_burn_rate_projection_and_downsampling(self):
        series = [(NOW - 3 * 3600 + i * 600, 80.0 + i * (2.0 / 6)) for i in range(19)]  # 2%/h for 3h
        rate = limits.burn_rate(series, NOW, 6 * 3600)
        self.assertAlmostEqual(rate, 2.0, places=2)
        projected = limits.projection(90.0, 2.0, NOW, NOW + 10 * 3600)
        self.assertEqual((projected["exhausts_in_seconds"], projected["exhausts_before_reset"]), (5 * 3600, True))
        self.assertFalse(limits.projection(90.0, 2.0, NOW, NOW + 3600)["exhausts_before_reset"])
        self.assertIsNone(limits.burn_rate(series[:1], NOW, 6 * 3600))
        self.assertIsNone(limits.projection(90.0, None, NOW))
        self.assertEqual(limits.projection(100.0, 0.0, NOW)["exhausts_in_seconds"], 0)
        thin = limits.downsample(series, 5)
        self.assertEqual((len(thin), thin[0], thin[-1]), (5, series[0], series[-1]))

    def test_limit_report_adds_history_to_codex_windows(self):
        resets = NOW + 86400
        self.append("\n".join(json.dumps(codex_event(NOW - 3600 + i * 600, primary=window(50.0 + i, 10080, resets)))
                              for i in range(7)) + "\n")
        report = limits.limit_report(codex_pane(str(self.path)), now=NOW, codex_home=self.home,
                                     companion_home=Path(self.folder.name) / "none")
        weekly = report["windows"]["weekly"]
        self.assertEqual(len(weekly["history"]), 7)
        self.assertAlmostEqual(weekly["burn_percent_per_hour"], 6.0, places=1)
        self.assertTrue(weekly["projection"]["exhausts_before_reset"])
        # The window opened 6 days ago (resets in 1 of 7 days); 56% used over 144 hours.
        self.assertAlmostEqual(weekly["average_percent_per_hour"], 56.0 / 144, places=3)

    def test_matching_lines_are_found_by_byte_search_across_chunks(self):
        resets = NOW + 86400
        noise = json.dumps({"type": "response_item", "payload": {"text": "y" * 300}})
        body = []
        for i in range(40):
            body += [noise, json.dumps(codex_event(NOW - 4000 + i * 100, primary=window(float(i), 10080, resets)))]
        self.append("\n".join(body) + "\n")
        history = limits.CodexHistory(self.home, chunk=97).refresh(NOW)
        self.assertEqual([u for _, u in history.series("codex", "weekly", resets, 10080)],
                         [float(i) for i in range(40)])

    def test_companion_events_list_recent_rejections(self):
        home = Path(self.folder.name) / "companion"
        write_lines(home / "projects" / "-srv" / "44444444-aaaa-bbbb-cccc-dddddddddddd.jsonl",
                    [companion_rejection(NOW - 30 * 86400, "seven_day", NOW - 20 * 86400),
                     companion_rejection(NOW - 600, "five_hour", NOW + 3600)])
        events = limits.companion_events(companion_pane(), now=NOW, home=home)
        self.assertEqual([e["limit_type"] for e in events], ["five_hour"])


class ScreenTests(unittest.TestCase):
    def assess(self, pane, text, **kwargs):
        with tempfile.TemporaryDirectory() as folder:
            return limits.assess_limits(pane, text, now=NOW, codex_home=Path(folder) / "c",
                                        companion_home=Path(folder) / "d", grok_home=Path(folder) / "g",
                                        omp_home=Path(folder) / "o", **kwargs)

    def test_codex_warning_line_is_low(self):
        text = "\x1b[33m⚠ Heads up, you have less than 5% of your weekly limit left. Run /status\x1b[0m\n› Ask Codex"
        result = self.assess(codex_pane(), text, low_percent=10)
        self.assertEqual(result["state"], "low")
        self.assertEqual(result["windows"]["weekly"]["remaining_below_percent"], 5)

    def test_quoted_warning_text_is_ignored(self):
        text = "  the pane said you have less than 5% of your weekly limit left earlier\n"
        self.assertEqual(self.assess(codex_pane(), text)["state"], "unknown")

    def test_hit_limit_line_is_exhausted_without_fresher_file_evidence(self):
        self.assertEqual(self.assess(codex_pane(), "■ You've hit your usage limit. Upgrade to Pro\n")["state"],
                         "exhausted")
        self.assertEqual(self.assess(companion_pane(), "You've hit your limit · resets 3pm\n")["state"], "exhausted")

    def test_fresh_file_evidence_outranks_stale_scrollback(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "codex"
            path = home / "sessions" / "2026" / "09" / "15" / "rollout-f.jsonl"
            write_lines(path, [codex_event(NOW - 60, primary=window(20.0, 10080, NOW + 86400))])
            result = limits.assess_limits(codex_pane(str(path)), "■ You've hit your usage limit.\n", now=NOW,
                                          codex_home=home, companion_home=Path(folder) / "none")
        self.assertEqual(result["state"], "ok")

    def test_other_panes_are_unknown(self):
        self.assertEqual(limits.assess_limits({"coding_session": {"provider": "bash"}})["state"], "unknown")

    def test_grok_and_qwen_limit_lines(self):
        self.assertEqual(self.assess(grok_pane(), "  You hit your weekly limit.\n")["state"], "exhausted")
        self.assertEqual(self.assess(grok_pane(), "You’ve reached your free Grok Build usage limit for now.\n")["state"],
                         "exhausted")
        self.assertEqual(self.assess(omp_pane(), "Error: 429 Allocated quota exceeded\n")["state"], "exhausted")
        self.assertEqual(self.assess(grok_pane(), "You've reached the model context limit\n")["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
