import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import claudex_team as team_module
import providers
from test_limits import codex_event, grok_billing, window, write_lines

NOW = 1789450000.0

# The team tab from `kilix @ ls` on a kilix engine with the sessions layout
# (claudex_team.py launch --placeholder; test- prefixes removed, worker_1's
# process added, other fields dropped).
TEAM_TAB = {
    "id": 5, "title": "claudex-team", "layout": "sessions",
    "layout_state": {
        "all_windows": {"window_groups": [{"id": 18, "window_ids": [18]}, {"id": 19, "window_ids": [19]},
                                          {"id": 20, "window_ids": [20]}, {"id": 21, "window_ids": [21]},
                                          {"id": 22, "window_ids": [22]}]},
        "class": "Sessions", "opts": {}},
    "windows": [{"id": 18, "title": "claudex-orchestrator"}, {"id": 19, "title": "claudex-companion"},
                {"id": 20, "title": "claudex_worker_1", "foreground_processes": [{"cmdline": ["grok", "--yolo"]}]},
                {"id": 21, "title": "claudex_worker_2"}, {"id": 22, "title": "claudex_worker_3"}]}


class FakeKilix:
    def __init__(self, ls=None, panes=None):
        self.calls, self._ls, self._panes, self.next_id = [], ls or [], panes or {}, 300

    def run(self, *args):
        self.calls.append(args)
        return ""

    def session(self, *args):
        self.calls.append(("session", *args))
        self.next_id += 1
        return {"status": "created", "pane": {"pane_id": self.next_id}}

    def ls(self):
        return self._ls

    def panes(self):
        return self._panes


def usage(grok=None, codex=None, checked_at=NOW - 60):
    providers_ = {}
    if grok is not None:
        providers_[providers.GROK_ID] = {"status": "ok", "checked_at": checked_at, "windows": grok}
    if codex is not None:
        providers_[providers.CODEX_ID] = {"status": "ok", "checked_at": checked_at, "windows": codex}
    return {"providers": providers_}


class TeamTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.codex_home, self.grok_home, self.qwen = root / "codex", root / "grok", root / "qwen-test"
        self.qwen.mkdir()
        for name in (providers.QWEN_OMP_LAUNCHER, providers.QWEN_COMPANION_LAUNCHER):
            (self.qwen / name).write_text("#!/bin/sh\n")
        self.environment = patch.dict(os.environ, {"CLAUDEX_QWEN_DIR": str(self.qwen)})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.folder.cleanup()

    def plan(self, usage_value):
        return team_module.make_plan(now=NOW, usage=usage_value, codex_home=self.codex_home, grok_home=self.grok_home)

    def check(self, kind, usage_value):
        return team_module.token_check(kind, now=NOW, usage=usage_value, codex_home=self.codex_home,
                                       grok_home=self.grok_home)


class LayoutTests(TeamTestCase):
    def test_the_team_tab_is_a_sessions_grid_in_launch_order(self):
        names = {member["title"]: member["role"] for member in team_module.team()}
        self.assertEqual(team_module.grid_order(TEAM_TAB, names), team_module.ORDER)
        self.assertTrue(team_module.layout_matches(TEAM_TAB, names))
        self.assertFalse(team_module.layout_matches(dict(TEAM_TAB, layout="splits"), names))
        groups = TEAM_TAB["layout_state"]["all_windows"]["window_groups"]
        swapped = {"all_windows": {"window_groups": [groups[1], groups[0], *groups[2:]]}}
        self.assertFalse(team_module.layout_matches(dict(TEAM_TAB, layout_state=swapped), names))


class TokenTests(TeamTestCase):
    def test_grok_is_exhausted_until_newer_evidence_shows_room(self):
        full = usage(grok=[{"name": "weekly", "label": "weekly", "used_percent": 100.0, "resets_at": NOW + 3600}])
        result = self.check("grok", full)
        self.assertEqual((result["state"], result["source"]), ("exhausted", "usage page"))
        write_lines(self.grok_home / "logs" / "unified.jsonl", [grok_billing(NOW - 10, 40.0, NOW - 86400, NOW + 86400)])
        result = self.check("grok", full)
        self.assertEqual((result["state"], result["source"]), ("available", "grok log"))

    def test_a_window_that_has_reset_is_not_evidence(self):
        stale = usage(grok=[{"name": "weekly", "used_percent": 100.0, "resets_at": NOW - 60}])
        self.assertEqual(self.check("grok", stale)["state"], "unknown")

    def test_spark_uses_its_own_windows_and_the_newest_source(self):
        page = usage(codex=[{"name": "weekly", "label": "weekly", "used_percent": 100.0, "resets_at": NOW + 3600},
                            {"name": "gpt-5.3-codex-spark_five_hour", "label": "GPT-5.3-Codex-Spark five_hour",
                             "used_percent": 0.0, "resets_at": NOW + 3600}])
        self.assertEqual(self.check("spark", page)["state"], "available")
        write_lines(self.codex_home / "sessions" / "2026" / "09" / "15" / "rollout-spark.jsonl",
                    [codex_event(NOW - 5, limit_id=team_module.SPARK_LIMIT_ID, primary=window(100.0, 300, NOW + 1200))])
        result = self.check("spark", page)
        self.assertEqual((result["state"], result["source"]), ("exhausted", "codex rollout"))


class PlanTests(TeamTestCase):
    def test_primaries_run_unless_their_tokens_are_exhausted(self):
        plan = {member["role"]: member for member in self.plan({})}
        self.assertEqual(plan["worker_1"]["chosen"]["argv"], ["grok", "--yolo"])
        self.assertEqual(plan["worker_3"]["chosen"]["argv"], ["codex", "--yolo", "-m", "gpt-5.3-codex-spark"])
        self.assertEqual(plan["orchestrator"]["chosen"]["argv"],
                         ["codex", "--yolo", "-m", "gpt-6-astra", "-c", "model_reasoning_effort=max"])
        self.assertIn(providers.COMPANION_TEAM_MODEL, plan["companion"]["chosen"]["argv"])
        worker_2 = plan["worker_2"]["chosen"]["argv"]
        self.assertEqual(worker_2, [str(self.qwen / providers.QWEN_COMPANION_LAUNCHER), "--here", "max", "--",
                                    providers.COMPANION_SKIP_PERMISSIONS])

        exhausted = usage(grok=[{"name": "weekly", "used_percent": 100.0, "resets_at": NOW + 3600}],
                          codex=[{"name": "spark", "label": "GPT-5.3-Codex-Spark weekly", "used_percent": 100.0,
                                  "resets_at": NOW + 3600}])
        plan = {member["role"]: member for member in self.plan(exhausted)}
        omp = [str(self.qwen / providers.QWEN_OMP_LAUNCHER), "--here", "max", "--", "--approval-mode=yolo"]
        self.assertEqual((plan["worker_1"]["chosen"]["argv"], plan["worker_3"]["chosen"]["argv"]), (omp, omp))
        self.assertEqual(plan["worker_1"]["checks"][0]["state"], "exhausted")

    def test_a_missing_launcher_is_reported_and_blocks_launch(self):
        (self.qwen / providers.QWEN_COMPANION_LAUNCHER).unlink()
        plan = self.plan({})
        self.assertEqual([m["role"] for m in plan if m["missing"]], ["worker_2"])
        with self.assertRaisesRegex(team_module.TeamError, "launcher not found"):
            team_module.launch(FakeKilix(), plan, cwd=Path("/srv/work"))


class LaunchTests(TeamTestCase):
    def test_panes_join_one_session_grid_in_order(self):
        kilix = FakeKilix()
        plan = self.plan({})
        result = team_module.launch(kilix, plan, cwd=Path("/srv/work"))
        adds = [call[1:] for call in kilix.calls if call[0] == "session"]
        self.assertEqual(result["window_ids"], {"orchestrator": 301, "companion": 302, "worker_1": 303,
                                                "worker_2": 304, "worker_3": 305})
        self.assertEqual([call[:4] for call in adds], [("grid", "add", "--name", "claudex-team")] * 5)
        self.assertEqual([call[4:6] for call in adds], [("--new", "--title")] + [("--into", "301")] * 4)
        for call, member in zip(adds, ("claudex-orchestrator", "claudex-companion", "claudex_worker_1",
                                       "claudex_worker_2", "claudex_worker_3")):
            self.assertEqual(call[call.index("--title") + 1], member)
            self.assertEqual(call[call.index("--cwd") + 1], "/srv/work")
            self.assertIn("--env", call)
            self.assertNotIn("--no-hold", call)
        self.assertEqual(adds[2][adds[2].index("--") + 1:], ("grok", "--yolo"))
        self.assertEqual(adds[2][adds[2].index("--var") + 1], "claudex_team=worker_1")
        self.assertEqual(kilix.calls[-1], ("focus-window", "--match", "id:301"))

    def test_existing_titles_block_a_real_launch_but_not_a_placeholder(self):
        kilix = FakeKilix(ls=[{"tabs": [TEAM_TAB]}])
        with self.assertRaisesRegex(team_module.TeamError, "claudex-orchestrator"):
            team_module.launch(kilix, self.plan({}), cwd=Path("/srv/work"))
        result = team_module.launch(kilix, self.plan({}), cwd=Path("/srv/work"), placeholder=True, background=True)
        adds = [call[1:] for call in kilix.calls if call[0] == "session"]
        self.assertEqual(adds[0][2:5], ("--name", "claudex-team-test", "--new"))
        self.assertEqual(adds[-1][adds[-1].index("--title") + 1], "test-claudex_worker_3")
        self.assertEqual(adds[0][-3:-1], ("bash", "-c"))
        self.assertNotIn("focus-window", [call[0] for call in kilix.calls])
        self.assertEqual(result["status"], "launched")

    def test_dry_run_runs_nothing(self):
        kilix = FakeKilix()
        result = team_module.launch(kilix, self.plan({}), cwd=Path("/srv/work"), dry_run=True)
        self.assertEqual((result["status"], kilix.calls), ("planned", []))
        self.assertEqual(result["commands"][4]["kilix"][:4], ["kilix", "session", "grid", "add"])


class StatusTests(TeamTestCase):
    def test_status_finds_the_panes_and_checks_the_layout(self):
        panes = {20: {"pane_id": 20, "activity": "working", "coding_session": None,
                       "process": {"name": "grok", "argv": ["grok", "--yolo"]}}}
        value = team_module.status(FakeKilix(ls=[{"tabs": [TEAM_TAB]}], panes=panes), now=NOW, usage={},
                                   codex_home=self.codex_home, grok_home=self.grok_home)
        self.assertEqual(value["status"], "ok")
        self.assertTrue(value["layouts"][0]["matches"])
        self.assertEqual(value["layouts"][0]["order"], list(team_module.ORDER))
        worker_1 = next(m for m in value["members"] if m["role"] == "worker_1")["windows"][0]
        self.assertEqual((worker_1["agent"], worker_1["command"], worker_1["activity"]), ("grok", "grok --yolo", "working"))

        partial = dict(TEAM_TAB, windows=[w for w in TEAM_TAB["windows"] if w["id"] != 22])
        value = team_module.status(FakeKilix(ls=[{"tabs": [partial]}]), now=NOW, usage={},
                                   codex_home=self.codex_home, grok_home=self.grok_home)
        self.assertEqual(value["status"], "incomplete")

    def test_placeholder_status_checks_the_test_panes(self):
        windows = [dict(w, title="test-" + w["title"]) for w in TEAM_TAB["windows"]]
        kilix = FakeKilix(ls=[{"tabs": [dict(TEAM_TAB, title="claudex-team-test", windows=windows)]}])
        self.assertEqual(team_module.status(kilix, now=NOW, usage={}, codex_home=self.codex_home,
                                            grok_home=self.grok_home)["status"], "incomplete")
        value = team_module.status(kilix, now=NOW, usage={}, codex_home=self.codex_home,
                                   grok_home=self.grok_home, placeholder=True)
        self.assertEqual(value["status"], "ok")


if __name__ == "__main__":
    unittest.main()
