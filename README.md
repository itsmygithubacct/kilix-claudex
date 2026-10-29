# Claudex

A CLI for launching, messaging and monitoring coding sessions in Kilix, with
usage-based routing, configurable teams and an editable periodic prompt timer.
Requires Python 3.10+ on Linux and Kilix Pane
Center. Run inside Kilix so the terminal connection settings are inherited.

Routes and the team launcher require a Kilix build with `session grid add`
and the engine's `sessions` layout. Older builds using only `kilix @ launch`
are not supported by these launchers. Python uses only its standard library.

## Setup

From this checkout:

```sh
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/claudex" "$HOME/.local/bin/claudex"
claudex init
claudex --help
```

Use `./claudex` directly if an installed command already exists. Add
`~/.local/bin` to PATH when using the link. `init` creates missing configuration
and prompt files without replacing existing data. A new timer is paused;
initialization does not start a daemon or contact Kilix. It honors the same
configuration overrides as the other commands.

Install the external tools for the commands you use:

| Feature | Prerequisites |
| --- | --- |
| Pane messaging, limits and timer | Kilix and Pane Center (`kilix-panes` or `kilix panes`) |
| Routes | Selected agent CLIs on PATH, with account access to the configured models |
| Codex and companion usage | `tb` from tmux-tui, tmux, and the respective signed-in CLI |
| Grok usage | The signed-in Grok CLI and network access to its billing endpoint |
| Qwen usage | Chrome/Chromium; Xvfb for the default virtual display |
| Team grid (1–12 sessions) | Kilix session grids and the selected agent CLIs on PATH |

Model IDs and effort names are configurable per team session; routes use the
presets documented below. Availability depends on your account and CLI version.
Routes and team commands launch agents with approval bypass
flags; use them only for work you have authorized with that level of access.

Private app data defaults to `~/.local/gpu_terminal/kilix-apps/kilix-claudex/`.
`GPU_TERMINAL_HOME` overrides the `~/.local/gpu_terminal` root. Configuration is
`loop.json`, the editable prompt is `prompt.txt`, and state/logs/locks are under
`.loop/` in that app directory. New runtime directories use mode 0700 and files
use 0600. `CLAUDEX_CONFIG` selects another configuration; `--config FILE` takes
precedence, including for isolated tests.
Relative prompt paths and runtime files use the resolved configuration file's
directory. A configuration symlink therefore shares the original directory's
state and locks. Runtime writes do not depend on the source checkout.

## Panes and messages

```sh
claudex panes
claudex read companion --lines 30
claudex monitor companion --watch 5
claudex idle orchestrator
claudex send companion 'Review the latest changes and report any blockers.'
claudex send companion --allow-busy 'FYI: the requested report is ready.'
claudex verify
```

Aliases `orchestrator` and `companion` select pane titles
`claudex-orchestrator` and `claudex-companion`. Numeric pane IDs and unique
Kilix labels/session prefixes also work. `panes --all` includes other panes.
Do not use a page title to identify a pane.

Sends normally require idle input. `--allow-busy` permits an intentional queued
message while work is in progress; it still blocks drafts, queued input and
dialogs. For a working Codex pane advertising `tab to queue message`, delivery
uses Tab and checks for a visible queue or recorded turn. Otherwise it uses
Enter. If a send without `--allow-busy` finds the target busy after its text
was placed and read back exactly, it deletes exactly those characters with DEL.
It then reports `skipped` (exit 2) once the composer is verified empty. If a
dialog opened instead, no further keys are sent. An unresponsive key, an
unverified withdrawal or text stranded in the composer reports an error without
retrying. Omit the text argument to read a message from stdin. Reading and
monitoring do not send messages.

Delivery holds an advisory per-pane lock through text placement, full-message
readback, a separately accepted submission key, and a final readback. The visible
composer must contain only the intended message. Readback ignores screen wrapping,
whitespace and Kilix's blank/single-dot braille decorations; it is not a
byte-for-byte transport check. Multi-dot braille letters in a draft block sends.
A single-dot braille draft cannot be distinguished reliably from decoration.
If placement cannot be verified, the error warns that text may remain in the
composer for inspection.
Raw terminal input from a sender that ignores this lock can still interleave.
Each send checks the pane,
broker and coding-session identity.

The CLI appends a unique `[cx-…]` marker. Short single-line messages are sent
inline. Prompts over 750 UTF-8 bytes, containing newline/CR/tab or braille, with
leading/trailing whitespace, or starting with `/`, `!`, `@`, `?` or `#` become exact
UTF-8 snapshot files. This also protects text that a TUI could interpret as a
command or shortcut. The receiver gets a
reference. The final wire message is at most 900 bytes. Terminal controls,
including DEL/C1 and directional formatting controls, are rejected. Unsafe
control characters in a snapshot path also block delivery.

`pending` means placement was verified but the recorded user turn is not yet
visible. `verify` rechecks the latest send without retransmission. If a newer
send finishes during verification, its evidence is preserved and the older
verification result reports `is_latest: false`. Companion pane metadata may leave
this pending; inspect `read companion` for the visible
result. Delivery evidence does not prove the receiver completed the task.
New send records include the exact wire message. A recorded turn must match it;
quoting its marker in a different message is insufficient. Confirmed observations
are retained when later turns arrive. If verification first runs after the
matching turn has left the latest-turn metadata, delivery can remain unconfirmed.
`submission_key` and `queue_observed` distinguish queue placement from a
recorded turn. `screen_changed_after_submit` covers either key; the older
`screen_changed_after_enter` field is null when Tab was used.

## Token limits

```sh
claudex limits                     # both claudex panes
claudex limits orchestrator --low 5
claudex limits 182 190             # any pane ID, including grok and omp (Qwen) panes
```

`limits` prints JSON for each pane: its weekly, five-hour or monthly windows, `state`
(`exhausted`, `low`, `ok` or `unknown`), the evidence sources, and which
windows the agent doesn't report. It exits 2 when any pane is exhausted. It
reads local files and the screen only; it never types into a pane.

- **Codex:** rollout `token_count` events carry `rate_limits` snapshots with
  `used_percent`, `window_minutes` (300 = five-hour, 10080 = weekly) and
  `resets_at`. Limits are account-wide, so the newest snapshot of the pane's
  current limit bucket in any recent rollout wins (`CODEX_HOME`, default
  `~/.codex`). Some buckets report only a weekly window; the five-hour window
  appears where the bucket has one.
- **Companion agent:** it keeps no usage percentage locally. Transcripts record
  rejected requests with `quotaLimits` (`five_hour` or `seven_day`, reset
  time), so a companion window is exhausted until its reset, or unknown. Its
  configuration directory variable and default are defined in `providers.py`.
- **Grok:** Grok Build logs its credit allowance to
  `$GROK_HOME/logs/unified.jsonl` (default `~/.grok`) while a session is open:
  `creditUsagePercent` and the current weekly or monthly period with its end.
  Grok has no five-hour window. The newest record wins, so after grok closes the
  last observed value remains until the period ends.
- **Qwen:** Qwen Token Plan panes run `omp --model qwen-token-plan/...` (or
  omp's built-in `alibaba-token-plan`). omp records no usage percentage, so
  the local evidence is a rejected request (`stopReason: error` with a quota
  message) in its sessions under `$PI_CODING_AGENT_DIR/sessions` (default
  `~/.omp/agent`). A rejection counts until a later Token Plan request succeeds
  or its window has elapsed. The Token Plan's 7-day window starts at the first
  call; its five-hour limit wasn't enforced as of September 2026. `omp usage`
  reports Token Plan percentages only for an `alibaba-token-plan` account signed
  in with console cookies; claudex never handles those credentials.
- **Screen:** line-start warnings such as `less than 5% of your weekly limit
  left`, `hit your usage limit`, `You hit your weekly limit` or `Allocated
  quota exceeded`. Fresh file evidence outranks possibly stale scrollback.

Pane Center identifies Codex and companion panes. Grok and omp panes are
recognized from their foreground command, and an omp pane counts as Qwen when its
model (from `--model` or its newest session in that directory) belongs to a
Token Plan provider. Grok and Qwen panes are monitored for limits only; `send`,
`idle` and the timer support Codex and the companion agent.

`limits --history` adds the current window's usage history. It also adds burn
rate: a least-squares percent per hour over the last hour for five-hour windows
and the last 6 hours for weekly ones. It projects when the window runs out and
whether that happens before the reset, for Codex and Grok. The companion agent and Qwen get
their recent rejection events.

### Dashboard and official usage pages

```sh
claudex dashboard open             # start the dashboard if needed and open it with kilix open-url
claudex dashboard start --port 8797
claudex dashboard status
claudex dashboard stop
claudex dashboard serve            # foreground
claudex dashboard provider         # every agent's official usage page (URLs in providers.py)
claudex dashboard provider codex
claudex dashboard provider grok
```

The dashboard is a local, read-only page. It shows a bar, reset countdown,
burn rate, projection and history sparkline for each window of each target
pane (default: both claudex panes). It refreshes every 30 seconds, and the
report is cached for 15 seconds.

- **Loopback only:** it binds to 127.0.0.1 (default port 8797) and refuses
  requests whose Host header isn't loopback.
- **Read-only:** it serves only `GET /`, `/api/limits` and `/api/health`.
- **Hardened page:** a nonce Content-Security-Policy.
- **Incremental reads:** rollouts are read incrementally, so a long-running
  dashboard reads only appended data.
- **Lifecycle files:** the process record lives in `.loop/dashboard.json` and
  its log in `.loop/dashboard.log`.

`dashboard open` and `dashboard provider` open URLs the way `kilix open-url`
does.

- **With a desktop browser** (`google-chrome`, `chromium-browser` or
  `firefox-esr`), kilix opens an ordinary browser window with its usual
  profile, so existing logins to the usage pages apply. claudex runs it as a
  background Kilix process, which inherits Kilix's display environment and
  creates no tab.
- **Without one,** kilix renders the page inside a pane, and claudex opens a
  titled tab for it.

The official pages are the authoritative numbers, including the companion agent's
and Qwen Token Plan percentages, which aren't available locally. `/api/health` reports page, limits
and health request counts, so you can confirm a browser page is loaded and
polling.

A window that has reset since it was observed doesn't count. `unknown` never
blocks. `send` and every timer tick skip an exhausted target (`status:
skipped`, exit 2) before typing; `send --allow-limited` overrides this. The
optional `min_remaining_percent` in `loop.json` also skips targets below that
remaining percentage.

### Reading usage

Codex is read from a tmux session named `codex_usage`, and Claude from
`claude_usage`, not from the websites. Grok is read from the grok CLI login
(`grok login`), through the same billing request the CLI uses, not from the
website. Qwen still comes from its official usage page. `claudex usage` checks `tb ls` for that
exact session name and creates a missing one with `tb new`. `tb a <name>` is
the interactive form of that create: it attaches afterwards, needs a TTY, and
does not return, so the CLI leaves the session detached. Each agent is started
there when the pane is still a shell. Codex is launched with startup
self-update disabled, so a failed upgrade cannot exit before the TUI is up.
`/status` is submitted twice for Codex, and only the screen after the second
submission is kept. Claude is different: type `claude`, wait a couple of
seconds, submit `/status`, then press Right twice to reach Usage. When that
dialog is already open, the reading is refreshed with Left, then Right, and
the screen is dumped a second later. Qwen still comes from its official
usage page.

The two tmux sessions must each contain one detached pane dedicated to usage.
Readers lock each session under `${XDG_CACHE_HOME:-~/.cache}/claudex-status/`,
check the pane identity before operations, and refuse drafts or an attached
session. Detach after signing in. A readiness timeout or an unrecognized
status screen returns a provider error instead of sending more keys. Other
programs that ignore these locks can still race tmux input.

```sh
claudex usage login                # open the Qwen usage page; Grok signs in with `grok login`
claudex usage                      # one line: remaining 5h and weekly for codex, claude, grok, qwen
claudex usage codex claude         # those two sessions only, no browser
claudex usage grok                 # the grok CLI billing request, no browser
claudex usage manual codex claude  # enter percentages from the websites directly
claudex usage show                 # the last reading, without opening a browser or tmux
claudex usage status               # profile path, whether it's open, time of the last reading
claudex usage --json               # the same short reading as one JSON object
claudex usage --full               # every window, reset time, and both percentages
claudex usage --human              # table of the 5h and weekly rows
claudex usage --human --used       # that table as used percent
claudex usage --full --verbose     # full JSON plus the tmux /status screen
```

If an automatic read fails or yields no usable percentages, the command asks
for that provider's website percentages in the terminal. It prints the page
URL and asks for the 5-hour/current-session and weekly/all-models values. Grok
asks whether its displayed period is weekly or monthly and collects that value.
Codex defaults to percent **left**; the others default to percent **used**.
Enter values such as `35`, `35% used`, or `65% left`; an explicit label overrides
the default. Enter `unknown` for a window the website does not show. All-unknown
answers or cancellation stop routing before a pane opens. This does not open
a browser or sign in for you. `usage manual` skips automatic reads entirely.

Manual values are cached with their entry time and used by the same routing
algorithm. They have no inferred reset time. A noninteractive command returns
an `input_required` status and a `claudex usage manual PROVIDER` instruction on
stderr instead of waiting for input. Run that command in a terminal and retry.

- **Profile:** `usage-browser/` in the app data directory, mode 0700. It holds
  your login for the Qwen site; claudex never reads its cookies. Grok uses
  the `grok login` token in `$GROK_HOME/auth.json` and does not use this profile.
  Delete the directory to sign out of that site. Close the login window
  before reading, because Chrome can't open one profile twice. Codex signs in
  inside `codex_usage`. Claude signs in inside `claude_usage`.
- **Browser:** Chrome runs on a private Xvfb display by default.
  `--display screen` shows the window, and `--display headless` uses headless
  Chrome. claudex speaks to it over `--remote-debugging-pipe`, which opens no
  network port. Chrome marks such a session as automated, and claudex doesn't
  hide that; signing in happens in an ordinary window.
- **Data:** Codex numbers are parsed from the second `/status` screen. Claude
  numbers are parsed from the Usage page: Current session is the five-hour
  window, and Current week is the weekly window. Grok `creditUsagePercent`
  and `currentPeriod` come from the CLI billing response. Qwen comes from the
  JSON its page requests: `per5HourPercentage` and `per1WeekPercentage`.
- **Automatic read failures:** sign-in requirements, bot challenges, parse
  failures and provider errors trigger manual entry. `--debug` writes an
  outline of each page's JSON keys to `.loop/usage-debug-AGENT.json`, keeping
  numbers and timestamps and redacting text values and the page title.
  Debug output can still
  contain site-defined key names and URL paths; treat it as private runtime data.
- **Output:** the default is one line, so a model can read it without a
  large prompt. It lists remaining 5-hour and weekly percent for Codex,
  Claude, Grok, and Qwen, and leaves out model splits, reset times, and the
  tmux screen. An agent that is not signed in is left off that line;
  `--verbose` includes it. `--json` is that same line as one compact JSON object.
  `--full` is the detailed JSON, with both `remaining_percent` and
  `used_percent` on every window. `--human` prints a table of the short
  windows; `--full --human` prints every window. `--used` reports consumed
  percent, and `--remaining` is the default. The tmux screen is stored in
  the cache and printed only with `--full --verbose` or `--human --verbose`.
  The latest reading per agent is kept in `.loop/usage-pages.json`.
  Exit codes: 0 read, 2 a window is fully used, 1 manual input is still required
  or nothing could be read. Input requirements take precedence over exhaustion.

## Routing

`claudex route` opens one session selected by a named algorithm. It creates a
Kilix session grid named `claudex-route` (`kilix session grid add`). The grid
does not take focus, and the pane holds when its command exits.

When a route checks usage, each agent's numbers stay valid for 30 minutes:
a valid provider reading whose `checked_at` is younger than that is not read
again. Known window resets also invalidate the route cache. Failed readings
ask for manual website percentages as described above.
The dashboard's 15-second cache is a separate timer.

```sh
claudex route max                  # one session
claudex route worker low           # one worker, selected by settings/usage
claudex route worker mid
claudex route worker high
claudex route worker max
claudex route worker mid --provider claude  # override the configured provider
claudex route security             # one session, along a fallback chain
claudex route max --dry-run        # print the model; open nothing
claudex route max "fix src/app.py" --dry-run
claudex route max --json           # the choice, the reason, and the numbers used
```

`--dry-run` prints the model it would open and does not open a pane. A request
after the route name is checked before opening a pane. It parses when it names at least one
file path and every path is an existing file. A path starts with `/`, `~/`,
`./`, or `../`, or contains a slash. Quote a path that has spaces. Relative
paths resolve from `--cwd`. The next line is `parse ok` and the files, or
`parse fail` with `no file path`, `missing`, or `not a file`. A request that
does not parse exits 1 and opens nothing. Codex, Claude, and Grok receive a
parsed request as their initial prompt. `worker` takes the tier, then the
request: `claudex route worker low "fix src/app.py"`.

A dry-run that selects by usage still refreshes stale readings, which can
drive the dedicated tmux sessions, request billing data or ask for percentages.
Use `claudex route worker low --provider codex --dry-run` to check argument
handling without account reads. `claudex usage show` reads the cache only.

Pass an exact prompt as one quoted argument. Several request arguments are
joined with spaces. Put `--` before a prompt beginning with `-` (after the
worker tier when using a worker route). The prompt is forwarded as one
positional argument, including its whitespace and line endings. The existing
file-path check validates references; it does not restrict what an agent may
do. A partial launch reports the panes already created; inspect them before
retrying so you do not launch duplicate work.

Max uses remaining percent and never chooses Qwen. A window explicitly entered
as `unknown` stays unknown. The checks run in this order:

1. Both weekly readings are known and each is under 20, and Grok still has
   weekly remaining: Grok's latest model at max, or xhigh when that model's
   menu has no max. grok-4.7's menu stops at xhigh.
2. One weekly reading leads the other by 50 points or more: that agent, at
   `gpt-6-astra` max or opus 5.5 max (`claude-opus-5-5`).
3. Claude's 5-hour reading is over 50: opus 5.5 max.
4. Otherwise `gpt-6-astra` max.

Worker opens exactly one pane. Set `routing.worker_provider` in `loop.json` to
`auto` (default), `codex` or `claude`; `--provider` overrides it for one command.
An explicit provider skips usage reads. In auto mode the Codex/Claude weekly
comparison, Claude 5-hour check and default above select the provider, then
the requested tier selects its model and effort. Auto workers do not use the
Grok branch of max routing.

| tier | Codex | Claude |
| --- | --- | --- |
| low | `gpt-6-luna` max | `claude-sonnet-5-5` low |
| mid | `gpt-6.1-terra` high | `claude-sonnet-5-5` medium |
| high | `gpt-6.1-sol` high | `claude-sonnet-5-5` high |
| max | `gpt-6.1-sol` max | `claude-sonnet-5-5` max |

Security opens one pane: `grok --yolo` while Grok has tokens, otherwise
opus 5.5 at high effort, otherwise `gpt-6-astra` at high effort. Out of
tokens means an account window (including a Grok monthly period) is fully used
and has not reset.
A signed-out agent is skipped.

Kilix verbs such as `ls`, `focus`, `new-pane`, and `watch` are `kilix`
commands. Route does not open a pane for them.

## Periodic prompts

```sh
claudex loop prompt 'Continue the current authorized task.'
claudex loop every 5
claudex loop resume
claudex loop start
claudex loop status
claudex loop pause
claudex loop stop
```

The self-prompt loop targets Codex; manual messaging supports both agents.
`start` launches a detached process; `run` runs in the foreground. The first
tick occurs after a full interval. `pause` disables sends while the daemon
remains available; `resume` starts a fresh interval. No boot service is installed.

Both the configuration and prompt reload while running. `loop prompt` reads
the current prompt; `loop prompt --file FILE` replaces it (`--file -` uses
stdin). Replacement and snapshots preserve whitespace and line endings. You
can also edit the files directly.

| Setting | Meaning |
| --- | --- |
| `target` | Pane title, numeric ID as a string, or unique session prefix. Stop and restart after changing it. |
| `interval_minutes` | At least 0.01; fractional minutes are supported. |
| `prompt_file` | UTF-8 file, relative to the configuration or absolute. |
| `enabled` | Boolean; false pauses the timer. |
| `min_remaining_percent` | Optional, 0–100 (default 0). Ticks skip a target with less remaining in any reported window. |

Busy panes, empty/unreadable prompts and held locks skip a tick without a
backlog. Invalid prompt controls and transient read failures before typing also
skip a tick. Invalid configuration suppresses sends until repaired. A changed
session identity or uncertain automatic delivery stops the daemon; inspect the
pane before restarting. `loop once` performs one tick while the daemon is
stopped. Periodic prompts can wake an agent even when nothing changed; pause
the timer when periodic work is no longer needed. The example prompt ends in a
newline and therefore uses a snapshot file.

## Team tab

`claudex team` opens 1–12 individually configured sessions in one Kilix tab
grid. Start with [examples/team.json](examples/team.json), edit it, then inspect
the fully resolved plan before launching:

```sh
claudex team plan examples/team.json --json
claudex team launch examples/team.json
```

The JSON file has `name`, shared `defaults`, and a `sessions` array. Each session
overrides the defaults independently:

| Field | Meaning |
| --- | --- |
| `provider` | `codex`, `claude`, `grok`, or `auto` (default). Auto uses the worker setting/algorithm. |
| `tier` | `low`, `mid`, `high`, `max`; defaults to `low`. Selects a worker preset for Codex/Claude. |
| `model` | Explicit model ID; requires an explicit provider. Overrides the tier preset. |
| `effort` | Effort passed to the selected CLI; overrides the preset. |
| `cwd` | Existing working directory, relative to the JSON file; defaults to `.`. |
| `prompt` | Common prompt in defaults, or a unique replacement for this session. Empty string clears it. |
| `title` | Session-only unique pane title; defaults to `NAME-NUMBER`. |
| `document` | Session-only existing file, relative to its cwd. Each assigned document must be distinct. |
| `prompt_addon` | Session-only instructions appended after the prompt and document reference. |

The initial prompt combines the exact base prompt, an instruction to read that
session's document by absolute path, and its add-on, separated by blank lines.
The agent reads the document from disk; Claudex does not copy its contents or
grant access beyond the session's existing filesystem permissions. No document
is required when the prompt alone is sufficient. Keep private team plans and
assignments outside a public checkout.

Use shared defaults for a uniform prompt, per-session `prompt` values for
unique instructions, or shared prompts with individual `prompt_addon` values.
When changing providers, also override any model/effort defaults that belong
to the previous provider. Model availability and effort support depend on the
installed CLI and account; the plan shows the exact arguments but cannot
verify access to a model.

`plan` opens no panes. Explicit Codex/Claude models do not read accounts;
`auto` may refresh usage and prompt for percentages. Grok's default model comes
from its local configuration. All paths, fields and the 1–12 count are checked
before account reads or launches. Unknown fields, duplicate titles and repeated
document paths (including symlinks) are rejected.

`launch` creates a new grid with the first session and adds the rest to that
same grid in list order, using Kilix's `sessions` layout. Each pane receives its
own cwd and the caller's PATH, holds after exit, and preserves the caller's
focus. Existing pane titles block launch. If a launch fails partway through,
the error lists confirmed pane IDs; inspect those panes before retrying.

## Runtime state

All runtime files live in `.loop/` beside the resolved configuration file:

| File | Purpose |
| --- | --- |
| `state.json` | PID, phase, identity, last attempt/fire and next-fire timestamps. |
| `loop.log` | Start, send, skip and error events. |
| `last-send.json` | Latest attempted delivery, exact wire message and evidence. |
| `last-send.lock` | Serializes brief evidence updates across sends and verification. |
| `send-PANE_ID.lock` | Shared advisory lock for the entire send transaction. |
| `dashboard.lock` | Prevents two servers from replacing one runtime's process record. |
| `usage-pages.lock` | Serializes updates of the shared provider usage cache. |
| `prompts/` | Immutable snapshots. Retained so pending messages can still read them; remove only after their work is finished. |

Cooperating senders must select the same configuration directory to share
locks. `CLAUDEX_KILIX_PANES` selects an alternate Pane Center executable.
Operational and unexpected errors return one JSON record on stdout (UTF-8
whatever the locale) and a diagnostic on stderr. A malformed `last-send.json`
is reported as an error rather than a traceback. A held
lock returns `status: skipped`, exit 2. Errors after a text-send attempt return
`status: uncertain`, exit 3, with the marker, attempted/accepted flags and
`text_may_remain`. Inspect the pane and use `verify` before considering a retry.
The attempt is saved in `last-send.json` when possible. Argument usage errors
retain argparse's usage output; foreground `loop run` also streams event logs.

Exit codes: 0 success/pending, 1 error before typing, 2 skipped/not idle,
3 uncertain delivery after a text attempt (including interruption), 130 interrupted
before a delivery attempt.

## Tests

```sh
make test
```

The regression suite covers input readiness, delivery ordering and readback,
session identity changes, concurrent verification, shared locking, exact prompt
files, first-run data initialization, live configuration reloads and the
detached timer lifecycle. It uses synthetic panes and a fake Kilix executable;
it does not type into a live terminal.
Dashboard tests bind temporary loopback ports. Usage-reader tests use scripted
tmux responses and temporary lock directories; no account login is required.
Team tests cover twelve-session grids, per-session documents/model settings,
exact prompt/add-on preservation, invalid plans and partial launch failures.
Manual usage tests cover website labels, failed reads and noninteractive errors.
