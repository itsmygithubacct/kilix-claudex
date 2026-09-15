"""Representative agent UI fixtures with synthetic session details."""
import unittest

from idleness import _visible, assess_idle, codex_queue_hint
from providers import COMPANION_ID, COMPANION_SURVEY_TEXT

RULE = "─" * 60
DUST = "⠁             ⠄          ⢀              ⠈"
FOOTER_CODEX = "  gpt-6-astra max · ~/example-project · Inspect changes"
FOOTER_COMPANION_IDLE = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
FOOTER_COMPANION_BUSY = ("  ⏵⏵ bypass permissions on (shift+tab to cycle) · "
                      "esc to interrupt · ← for agents")


def pane(agent="codex", activity="idle"):
    return {"pane_id": 1, "activity": activity, "process": {"name": agent},
            "coding_session": {"provider": agent, "live_status": activity}}


def codex(*above, composer="› Ask Codex to do anything          ⠂   ⠁"):
    return "\n".join([*above, DUST, composer, "       ⢀    ⠄", FOOTER_CODEX, ""])


def companion(*above, box=("❯ ",), footer=FOOTER_COMPANION_IDLE):
    return "\n".join([*above, RULE, *box, RULE, footer, ""])


CODEX_DONE = ("  Inspection only; no files changed.",
              "─ Worked for 3m 17s " + "─" * 30)
COMPANION_DONE = ("  The research note has the full record.",
               "✻ Churned for 24s · done 1:39 AM")


class CodexTests(unittest.TestCase):
    def test_braille_letters_in_a_draft_are_not_discarded_as_dust(self):
        v = assess_idle(pane(), codex(composer="› ⠓⠑⠇⠇⠕"), allow_busy=True)
        self.assertEqual((v["ready"], v["screen_state"]), (False, "occupied"))

    def test_tab_hint_below_composer_does_not_hide_stranded_input(self):
        screen = "\n".join(["• Working (4s • esc to interrupt)",
                             "› stranded message", "tab to queue message", FOOTER_CODEX])
        self.assertTrue(codex_queue_hint(screen))
        v = assess_idle(pane(activity="working"), screen, allow_busy=True)
        self.assertEqual((v["ready"], v["screen_state"]), (False, "occupied"))
        self.assertFalse(codex_queue_hint(codex("tab to queue message", *CODEX_DONE)))
        self.assertFalse(codex_queue_hint(codex(composer="› explain tab to queue message")))

    def test_idle_placeholder_plain(self):
        v = assess_idle(pane(), codex(*CODEX_DONE))
        self.assertTrue(v["ready"], v)
        self.assertEqual(v["state"], "idle")

    def test_braille_dust_touching_prompt_glyph(self):
        v = assess_idle(pane(), codex(composer="›⠁Ask Codex to do anything⡀   ⠁"))
        self.assertTrue(v["ready"], v)

    def test_idle_placeholder_ansi_faint(self):
        line = ("\x1b[m\x1b[22;1;38:2:248:183:85;48:2:56:57:59m›"
                "\x1b[22;38:2:130:133:130m⠁\x1b[22;2;39mAsk Codex to do anything"
                "\x1b[22m           \x1b[38:2:68:69:71m⠂\x1b[39m")
        v = assess_idle(pane(), codex(*CODEX_DONE, composer=line))
        self.assertTrue(v["ready"], v)

    def test_typed_text_ansi_is_occupied(self):
        line = "\x1b[m\x1b[1m›\x1b[22m fix the flaky test\x1b[m"
        v = assess_idle(pane(), codex(composer=line))
        self.assertEqual((v["ready"], v["state"]), (False, "occupied"))

    def test_typed_text_plain_is_occupied(self):
        v = assess_idle(pane(), codex(composer="› half-typed prompt from the user"))
        self.assertEqual((v["ready"], v["state"]), (False, "occupied"))

    def test_log_says_working(self):
        v = assess_idle(pane(activity="working"),
                        codex("• Working (4m 25s • esc to interrupt)"))
        self.assertEqual((v["ready"], v["state"]), (False, "working"))

    def test_screen_busy_beats_lagging_log(self):
        v = assess_idle(pane(activity="idle"),
                        codex("• Working (4s • esc to interrupt)"))
        self.assertEqual((v["ready"], v["state"]), (False, "working"))

    def test_log_working_screen_idle_flags_disagreement(self):
        v = assess_idle(pane(activity="working"), codex(*CODEX_DONE))
        self.assertFalse(v["ready"])
        self.assertIn("interrupted", v["reason"])

    def test_busy_indicator_does_not_hide_a_draft(self):
        # A queued FYI to a busy pane must not type into the user's draft.
        screen = codex("• Working (4m 25s • esc to interrupt)",
                       composer="› user is typing this")
        v = assess_idle(pane(activity="working"), screen)
        self.assertEqual(v["state"], "working")
        self.assertEqual(v["screen"]["state"], "occupied")
        v = assess_idle(pane(activity="idle"), screen)
        self.assertEqual((v["ready"], v["state"]), (False, "occupied"))

    def test_busy_indicator_does_not_hide_a_draft_ansi(self):
        line = "\x1b[m\x1b[1m›\x1b[22m draft\x1b[m"
        v = assess_idle(pane(activity="working"),
                        codex("• Working (9s • esc to interrupt)", composer=line))
        self.assertEqual(v["screen"]["state"], "occupied")

    def test_busy_indicator_does_not_hide_queued_input(self):
        v = assess_idle(pane(activity="working"), codex(
            "• Working (3m 02s • esc to interrupt)",
            "• Messages to be submitted after next tool call (press esc to "
            "interrupt and send immediately)",
            "  ↳ please also add tests"))
        self.assertEqual(v["screen"]["state"], "queued")

    def test_allow_busy_permits_only_a_clean_busy_pane(self):
        busy = "• Working (4m 25s • esc to interrupt)"
        v = assess_idle(pane(activity="working"), codex(busy), allow_busy=True)
        self.assertEqual((v["ready"], v["state"], v["screen_state"]),
                         (True, "working", "working"))
        # Screen busy while the log lags behind still counts as busy.
        v = assess_idle(pane(activity="idle"), codex(busy), allow_busy=True)
        self.assertTrue(v["ready"], v)
        v = assess_idle(pane(activity="working"),
                        codex(busy, composer="› unsent draft"), allow_busy=True)
        self.assertEqual((v["ready"], v["screen_state"]), (False, "occupied"))
        self.assertIn("allow_busy refused because the screen is occupied", v["reason"])
        v = assess_idle(pane(activity="working"), codex(
            busy, "• Messages to be submitted after next tool call",
            "  ↳ earlier FYI"), allow_busy=True)
        self.assertEqual((v["ready"], v["screen_state"]), (False, "queued"))

    def test_allow_busy_never_overrides_waiting_or_dead(self):
        v = assess_idle(pane(activity="waiting"), codex(), allow_busy=True)
        self.assertFalse(v["ready"])
        v = assess_idle(pane(activity="waiting"),
                        codex("• Working (4s • esc to interrupt)"), allow_busy=True)
        self.assertEqual((v["ready"], v["state"]), (False, "waiting"))
        v = assess_idle(pane(activity="working"), codex(
            "■ You've hit your usage limit."), allow_busy=True)
        self.assertEqual((v["ready"], v["state"]), (False, "dead"))

    def test_allow_busy_does_not_make_an_idle_pane_busy(self):
        v = assess_idle(pane(), codex(*CODEX_DONE), allow_busy=True)
        self.assertEqual((v["ready"], v["state"]), (True, "idle"))

    def test_queued_input(self):
        v = assess_idle(pane(), codex(
            "• Messages to be submitted after next tool call",
            "  ↳ please also add tests"))
        self.assertEqual((v["ready"], v["state"]), (False, "queued"))

    def test_quoted_marker_above_finished_turn_is_ignored(self):
        v = assess_idle(pane(), codex(
            '  Codex shows "esc to interrupt" and "• Working (" while busy.',
            *CODEX_DONE))
        self.assertTrue(v["ready"], v)

    def test_usage_limit_is_dead(self):
        v = assess_idle(pane(), codex(
            "■ You've hit your usage limit. Upgrade to Pro to continue."))
        self.assertEqual((v["ready"], v["state"]), (False, "dead"))

    def test_fresh_session_needs_screen_opt_in(self):
        screen = codex()
        v = assess_idle(pane(activity="agent"), screen)
        self.assertEqual((v["ready"], v["state"]), (False, "unknown"))
        v = assess_idle(pane(activity="agent"), screen, allow_screen_only=True)
        self.assertTrue(v["ready"], v)

    def test_no_composer(self):
        v = assess_idle(pane(), "some unrelated output\n")
        self.assertEqual((v["ready"], v["state"]), (False, "no-composer"))


class CompanionTests(unittest.TestCase):
    def test_allow_busy_cannot_override_waiting_with_a_stale_busy_footer(self):
        v = assess_idle(pane(COMPANION_ID, "waiting"), companion(footer=FOOTER_COMPANION_BUSY),
                        allow_busy=True)
        self.assertEqual((v["ready"], v["state"]), (False, "waiting"))

    def test_idle_box(self):
        v = assess_idle(pane(COMPANION_ID), companion(*COMPANION_DONE))
        self.assertTrue(v["ready"], v)

    def test_busy_footer(self):
        v = assess_idle(pane(COMPANION_ID, "idle"), companion(
            "· Frolicking… (41m 0s · ↓ 194.0k tokens)", footer=FOOTER_COMPANION_BUSY))
        self.assertEqual((v["ready"], v["state"]), (False, "working"))

    def test_queued(self):
        v = assess_idle(pane(COMPANION_ID), companion(
            box=("❯ Press up to edit queued messages",)))
        self.assertEqual((v["ready"], v["state"]), (False, "queued"))

    def test_busy_footer_does_not_hide_a_draft(self):
        v = assess_idle(pane(COMPANION_ID, "working"), companion(
            box=("❯ user draft in progress",), footer=FOOTER_COMPANION_BUSY))
        self.assertEqual(v["state"], "working")
        self.assertEqual(v["screen"]["state"], "occupied")

    def test_allow_busy_companion_draft_blocks(self):
        v = assess_idle(pane(COMPANION_ID, "working"), companion(
            box=("❯ user draft",), footer=FOOTER_COMPANION_BUSY), allow_busy=True)
        self.assertEqual((v["ready"], v["screen_state"]), (False, "occupied"))
        v = assess_idle(pane(COMPANION_ID, "working"), companion(
            footer=FOOTER_COMPANION_BUSY), allow_busy=True)
        self.assertTrue(v["ready"], v)

    def test_busy_footer_does_not_hide_queued_input(self):
        v = assess_idle(pane(COMPANION_ID, "working"), companion(
            box=("❯ Press up to edit queued messages",),
            footer=FOOTER_COMPANION_BUSY))
        self.assertEqual(v["screen"]["state"], "queued")

    def test_occupied_multiline(self):
        v = assess_idle(pane(COMPANION_ID), companion(
            box=("❯ first line of a draft", "  second line")))
        self.assertEqual((v["ready"], v["state"]), (False, "occupied"))

    def test_question_menu_replaces_box(self):
        screen = "\n".join([
            "  2. Speech models only",
            "  3. Type something.",
            RULE,
            "  4. Chat about this",
            "Enter to select · ↑/↓ to navigate · Esc to cancel", ""])
        v = assess_idle(pane(COMPANION_ID, "waiting"), screen)
        self.assertEqual((v["ready"], v["state"]), (False, "waiting"))
        v = assess_idle(pane(COMPANION_ID, "idle"), screen)
        self.assertEqual((v["ready"], v["state"]), (False, "waiting"))

    def test_feedback_survey(self):
        v = assess_idle(pane(COMPANION_ID), companion(
            *COMPANION_DONE, f"● {COMPANION_SURVEY_TEXT}? (optional)",
            "  1: Bad    2: Fine   3: Good   0: Dismiss"))
        self.assertEqual((v["ready"], v["state"]), (False, "survey"))


class ComposerEmptyTests(unittest.TestCase):
    def test_only_a_placeholder_or_blank_composer_counts_as_empty(self):
        from idleness import composer_is_empty
        rule = "─" * 40
        self.assertTrue(composer_is_empty("codex", "─ Worked for 1m ─────\n› Ask Codex to do anything\n  model · x\n"))
        self.assertTrue(composer_is_empty("codex", "› ⠁ Ask Codex to do anything ⡀\n  model · x\n"))
        self.assertFalse(composer_is_empty("codex", "› leftover [cx-1234]\n  model · x\n"))
        self.assertFalse(composer_is_empty("codex", "no composer on this screen\n"))
        self.assertTrue(composer_is_empty(COMPANION_ID, f"{rule}\n❯ \n{rule}\n  footer\n"))
        self.assertFalse(composer_is_empty(COMPANION_ID, f"{rule}\n❯ draft\n{rule}\n  footer\n"))


class GeneralTests(unittest.TestCase):
    def test_gone(self):
        self.assertEqual(assess_idle(None, "")["state"], "gone")

    def test_shell_is_unsupported(self):
        shell = {"activity": "shell", "process": {"name": "bash"},
                 "coding_session": None}
        self.assertEqual(assess_idle(shell, "$ ")["state"], "unsupported")

    def test_empty_capture(self):
        v = assess_idle(pane(), "")
        self.assertEqual((v["ready"], v["state"]), (False, "no-screen"))

    def test_truecolor_semicolon_form_is_not_faint(self):
        self.assertEqual(_visible("\x1b[38;2;2;2;2mtyped\x1b[m")[1], "typed")
        self.assertEqual(_visible("\x1b[2mghost\x1b[22mreal")[1], "real")


if __name__ == "__main__":
    unittest.main()
