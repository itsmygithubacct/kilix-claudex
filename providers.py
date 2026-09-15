"""Coding agents this app drives, identified the way Kilix Pane Center names them.

Every product-specific literal lives here, so the rest of the code can stay
provider-neutral. The orchestrator agent is Codex; the companion agent is the
other supported coding CLI. Grok and Qwen panes are monitored for limits only.
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

# Agents Pane Center doesn't track, recognized from their foreground command.
# Sends and idle detection stay limited to SUPPORTED.
GROK_ID = "grok"
QWEN_ID = "qwen"
LIMIT_PROVIDERS = (*SUPPORTED, GROK_ID, QWEN_ID)
LABELS.update({GROK_ID: "Grok", QWEN_ID: "Qwen"})
USAGE_PAGES.update({
    GROK_ID: "https://grok.com/?_s=usage",
    QWEN_ID: "https://modelstudio.console.alibabacloud.com/ap-southeast-1/subscription/token-plan/personal",
})
GROK_HOME_ENV = "GROK_HOME"
GROK_HOME_DIR = ".grok"
OMP_COMMAND = "omp"
OMP_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
OMP_AGENT_DIR = ".omp/agent"
# omp providers that bill a Qwen Token Plan: a models.yml custom provider and omp's built-in one.
QWEN_OMP_PROVIDERS = ("qwen-token-plan", "alibaba-token-plan")
