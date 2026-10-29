import json
import unittest
import urllib.error

import grok_status
import usage_pages


BODY = {"config": {"creditUsagePercent": 59.0, "currentPeriod": {
    "type": "USAGE_PERIOD_TYPE_WEEKLY",
    "start": "2026-10-21T17:11:41+00:00",
    "end": "2026-10-28T17:11:41+00:00"},
    "productUsage": [{"product": "Build", "usagePercent": 59.0}]}}


class TokenTests(unittest.TestCase):
    def test_missing_file_and_empty_file_have_no_token(self):
        self.assertIsNone(grok_status.access_token("/no/such/home"))

    def test_newest_key_is_chosen_without_reading_other_fields(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "auth.json").write_text(json.dumps({
                "old": {"key": "old-token", "expires_at": "2026-01-01T00:00:00Z"},
                "new": {"key": "new-token", "expires_at": "2026-10-28T00:00:00Z", "email": "hidden"},
            }))
            self.assertEqual(grok_status.access_token(folder), "new-token")


class ReadTests(unittest.TestCase):
    def fetch(self, status, body):
        def run(token, version):
            self.seen = (token, version)
            return status, body
        return run

    def test_billing_json_becomes_the_weekly_window(self):
        entry = grok_status.read(now=1_000.0, home=self._home("token"), fetch=self.fetch(200, BODY))
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["source"], grok_status.SOURCE)
        self.assertEqual([(item["name"], item["used_percent"], item["remaining_percent"])
                          for item in entry["windows"]],
                         [("weekly", 59.0, 41.0), ("weekly_build", 59.0, 41.0)])
        self.assertEqual(usage_pages.summary_record({"providers": {"grok": entry}})["grok"], {"weekly": 41})

    def test_no_login_does_not_call_the_network(self):
        def fetch(token, version):
            raise AssertionError("fetch")
        entry = grok_status.read(now=1_000.0, home="/none", fetch=fetch)
        self.assertEqual(entry["status"], "login_required")
        self.assertIn("grok login", entry["reason"])

    def test_rejected_login_and_bad_status_are_entries(self):
        home_token = self._home("token")
        denied = grok_status.read(now=1_000.0, home=home_token, fetch=self.fetch(401, {}))
        self.assertEqual(denied["status"], "login_required")
        self.assertEqual(self.seen[0], "token")
        failed = grok_status.read(now=1_000.0, home=home_token, fetch=self.fetch(500, {"config": {}}))
        self.assertEqual(failed["status"], "error")
        self.assertIn("500", failed["reason"])

    def test_transport_errors_stay_on_the_entry(self):
        def fetch(token, version):
            raise urllib.error.URLError("down")
        entry = grok_status.read(now=1_000.0, home=self._home("token"), fetch=fetch)
        self.assertEqual(entry["status"], "error")
        self.assertIn("network or protocol error", entry["reason"])

    def _home(self, token):
        import tempfile
        from pathlib import Path
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        Path(folder.name, "auth.json").write_text(json.dumps({"a": {"key": token}}))
        Path(folder.name, "version.json").write_text(json.dumps({"version": "1.0.70"}))
        return folder.name


if __name__ == "__main__":
    unittest.main()
