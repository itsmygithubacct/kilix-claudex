import http.client
import json
import re
import threading
import unittest

import dashboard


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.calls = 0
        self.fail = False

        def build():
            self.calls += 1
            if self.fail:
                raise RuntimeError("boom")
            return {"panes": [{"target": "orchestrator", "state": "low", "reason": "<script>x</script>",
                               "windows": {}}], "providers": dashboard.provider_links()}

        self.server = dashboard.make_server(build, 0, ttl=60)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, host="127.0.0.1"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", f"{host}:{self.port}")
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def test_health_counts_requests(self):
        response, body = self.request("GET", "/api/health")
        health = json.loads(body)
        self.assertEqual((response.status, health["ok"], health["requests"]["health"]), (200, True, 1))
        self.request("GET", "/")
        self.request("GET", "/api/limits")
        counts = json.loads(self.request("GET", "/api/health")[1])["requests"]
        self.assertEqual((counts["page"], counts["limits"], counts["health"]), (1, 1, 2))

    def test_health_and_cached_json_report(self):
        response, body = self.request("GET", "/api/health")
        self.assertEqual((response.status, json.loads(body)["ok"]), (200, True))
        for _ in range(2):
            response, body = self.request("GET", "/api/limits")
            report = json.loads(body)
            self.assertEqual((response.status, report["ok"], report["panes"][0]["state"]), (200, True, "low"))
        self.assertEqual(self.calls, 1)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")

    def test_page_uses_a_fresh_nonce_and_never_inlines_report_text(self):
        response, body = self.request("GET", "/")
        page = body.decode()
        nonce = re.search(r"script-src 'nonce-([^']+)'", response.getheader("Content-Security-Policy")).group(1)
        self.assertIn(f'<script nonce="{nonce}">', page)
        self.assertNotIn("<script>x</script>", page)
        self.assertNotIn("innerHTML", page)
        _, again = self.request("GET", "/")
        self.assertNotEqual(page, again.decode())

    def test_foreign_hosts_methods_and_paths_are_refused(self):
        self.assertEqual(self.request("GET", "/api/limits", host="evil.example")[0].status, 403)
        self.assertEqual(self.request("GET", "/api/limits", host="localhost")[0].status, 200)
        self.assertEqual(self.request("POST", "/api/limits")[0].status, 405)
        self.assertEqual(self.request("GET", "/nope")[0].status, 404)

    def test_a_failing_report_is_shown_not_fatal(self):
        self.fail = True
        server = dashboard.CachedReport(lambda: (_ for _ in ()).throw(RuntimeError("boom")), ttl=0)
        self.assertEqual(server.get()["ok"], False)
        self.assertIn("RuntimeError: boom", server.get()["error"])

    def test_only_loopback_binds_are_allowed(self):
        with self.assertRaises(ValueError):
            dashboard.make_server(lambda: {}, 0, host="0.0.0.0")


if __name__ == "__main__":
    unittest.main()
