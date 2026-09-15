import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import claudex_team as team_module
import providers
from test_limits import codex_event, grok_billing, window, write_lines

NOW = 1789450000.0

# The original claudex tab, as `kilix @ ls` reported it (splits layout).
ORIGINAL_TAB = {
    "id": 39, "title": "claudex-companion", "layout": "splits",
    "layout_state": {
        "all_windows": {"window_groups": [{"id": 64, "window_ids": [136]}, {"id": 97, "window_ids": [194]},
                                          {"id": 65, "window_ids": [137]}, {"id": 96, "window_ids": [193]},
                                          {"id": 98, "window_ids": [199]}]},
        "pairs": {"one": {"one": 64, "two": 65},
                  "two": {"one": 97, "two": {"horizontal": False, "one": 96, "two": 98}}}},
    "windows": [{"id": 136, "title": "claudex-orchestrator"}, {"id": 137, "title": "claudex-companion"},
                {"id": 194, "title": "claudex_worker_1", "foreground_processes": [{"cmdline": ["grok", "--yolo"]}]},
                {"id": 193, "title": "claudex_worker_2"}, {"id": 199, "title": "claudex_worker_3"}]}


class FakeKilix:
    def __init__(self, ls=None, panes=None):
        self.calls, self._ls, self._panes, self.next_id = [], ls or [], panes or {}, 300

    def run(self, *args):
        self.calls.append(args)
        if args[0] == "launch":
            self.next_id += 1
            return str(self.next_id)
        return ""

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
    def test_the_original_tab_has_the_team_shape(self):
        names = {member["title"]: member["role"] for member in team_module.team()}
        self.assertEqual(team_module.layout_shape(ORIGINAL_TAB, names), team_module.EXPECTED_SHAPE)


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
    def test_panes_open_in_order_with_the_splits_that_rebuild_the_layout(self):
        kilix = FakeKilix()
        plan = self.plan({})
        result = team_module.launch(kilix, plan, cwd=Path("/srv/work"))
        launches = [call for call in kilix.calls if call[0] == "launch"]
        self.assertEqual(result["window_ids"], {"orchestrator": 301, "worker_1": 302, "companion": 303,
                                                "worker_2": 304, "worker_3": 305})
        self.assertIn("--type=tab", launches[0])
        self.assertIn("--tab-title=claudex-team", launches[0])
        placements = [(call[call.index("--next-to") + 1], [a for a in call if a.startswith("--location=")][0])
                      for call in launches[1:]]
        self.assertEqual(placements, [("id:301", "--location=vsplit"), ("id:301", "--location=vsplit"),
                                      ("id:302", "--location=vsplit"), ("id:304", "--location=hsplit")])
        for call, member in zip(launches, ("claudex-orchestrator", "claudex_worker_1", "claudex-companion",
                                           "claudex_worker_2", "claudex_worker_3")):
            self.assertIn(f"--title={member}", call)
            self.assertIn("--hold", call)
            self.assertIn("--cwd=/srv/work", call)
        self.assertEqual(launches[1][-2:], ("grok", "--yolo"))
        self.assertEqual(kilix.calls[-1], ("focus-window", "--match", "id:301"))

    def test_existing_titles_block_a_real_launch_but_not_a_placeholder(self):
        kilix = FakeKilix(ls=[{"tabs": [ORIGINAL_TAB]}])
        with self.assertRaisesRegex(team_module.TeamError, "claudex-orchestrator"):
            team_module.launch(kilix, self.plan({}), cwd=Path("/srv/work"))
        result = team_module.launch(kilix, self.plan({}), cwd=Path("/srv/work"), placeholder=True, background=True)
        launches = [call for call in kilix.calls if call[0] == "launch"]
        self.assertIn("--tab-title=claudex-team-test", launches[0])
        self.assertIn("--title=test-claudex_worker_3", launches[-1])
        self.assertEqual(launches[0][-3:-1], ("bash", "-c"))
        self.assertNotIn("focus-window", [call[0] for call in kilix.calls])
        self.assertEqual(result["status"], "launched")

    def test_dry_run_runs_nothing(self):
        kilix = FakeKilix()
        result = team_module.launch(kilix, self.plan({}), cwd=Path("/srv/work"), dry_run=True)
        self.assertEqual((result["status"], kilix.calls), ("planned", []))
        self.assertEqual(result["commands"][4]["kilix"][:3], ["kilix", "@", "launch"])


class StatusTests(TeamTestCase):
    def test_status_finds_the_panes_and_checks_the_layout(self):
        panes = {194: {"pane_id": 194, "activity": "working", "coding_session": None,
                       "process": {"name": "grok", "argv": ["grok", "--yolo"]}}}
        value = team_module.status(FakeKilix(ls=[{"tabs": [ORIGINAL_TAB]}], panes=panes), now=NOW, usage={},
                                   codex_home=self.codex_home, grok_home=self.grok_home)
        self.assertEqual(value["status"], "ok")
        self.assertTrue(value["layouts"][0]["matches"])
        worker_1 = next(m for m in value["members"] if m["role"] == "worker_1")["windows"][0]
        self.assertEqual((worker_1["agent"], worker_1["command"], worker_1["activity"]), ("grok", "grok --yolo", "working"))

        partial = dict(ORIGINAL_TAB, windows=[w for w in ORIGINAL_TAB["windows"] if w["id"] != 199])
        value = team_module.status(FakeKilix(ls=[{"tabs": [partial]}]), now=NOW, usage={},
                                   codex_home=self.codex_home, grok_home=self.grok_home)
        self.assertEqual(value["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
