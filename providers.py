"""Coding agents this app drives, identified the way Kilix Pane Center names them.

Every product-specific literal lives here, so the rest of the code can stay
provider-neutral. The orchestrator agent is Codex; the companion agent is the
other supported coding CLI.
"""

CODEX_ID = "codex"
COMPANION_ID = "claude"
COMPANION_LABEL = "Claude"
SUPPORTED = (CODEX_ID, COMPANION_ID)
LABELS = {CODEX_ID: "Codex", COMPANION_ID: COMPANION_LABEL}

# Where each agent keeps its local session files.
CODEX_HOME_ENV = "CODEX_HOME"
CODEX_HOME_DIR = ".codex"
COMPANION_CONFIG_ENV = "CLAUDE_CONFIG_DIR"
COMPANION_HOME_DIR = ".claude"

# Official account usage pages.
USAGE_PAGES = {
    CODEX_ID: "https://chatgpt.com/codex/settings/usage",
    COMPANION_ID: "https://claude.ai/settings/usage",
}

# On-screen text of the companion agent's optional feedback survey.
COMPANION_SURVEY_TEXT = "How is Claude doing this session"
