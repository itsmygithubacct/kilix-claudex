# Claudex

A CLI for messaging and monitoring coding session panes in Kilix, with an
editable periodic prompt timer. Requires Python 3.10+ on Linux and Kilix Pane
Center. Run inside Kilix so the terminal connection settings are inherited.

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
```

`limits` prints JSON for each pane: its weekly and five-hour windows, `state`
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
- **Screen:** line-start warnings such as `less than 5% of your weekly limit
  left` or `hit your usage limit`. Fresh file evidence outranks possibly stale
  scrollback.

`limits --history` adds the current window's usage history. It also adds burn
rate: a least-squares percent per hour over the last hour for five-hour windows
and the last 6 hours for weekly ones. It projects when the window runs out and
whether that happens before the reset. The companion agent gets its recent rejection events.

### Dashboard and official usage pages

```sh
claudex dashboard open             # start the dashboard if needed and open it with kilix open-url
claudex dashboard start --port 8797
claudex dashboard status
claudex dashboard stop
claudex dashboard serve            # foreground
claudex dashboard provider         # both agents' official usage pages (URLs in providers.py)
claudex dashboard provider codex
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
percentages, which aren't available locally. `/api/health` reports page, limits
and health request counts, so you can confirm a browser page is loaded and
polling.

A window that has reset since it was observed doesn't count. `unknown` never
blocks. `send` and every timer tick skip an exhausted target (`status:
skipped`, exit 2) before typing; `send --allow-limited` overrides this. The
optional `min_remaining_percent` in `loop.json` also skips targets below that
remaining percentage.

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

## Runtime state

All runtime files live in `.loop/` beside the resolved configuration file:

| File | Purpose |
| --- | --- |
| `state.json` | PID, phase, identity, last attempt/fire and next-fire timestamps. |
| `loop.log` | Start, send, skip and error events. |
| `last-send.json` | Latest attempted delivery, exact wire message and evidence. |
| `last-send.lock` | Serializes brief evidence updates across sends and verification. |
| `send-PANE_ID.lock` | Shared advisory lock for the entire send transaction. |
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
3 uncertain delivery after a text attempt, 130 interrupted.

## Tests

```sh
make test
```

The regression suite covers input readiness, delivery ordering and readback,
session identity changes, concurrent verification, shared locking, exact prompt
files, first-run data initialization, live configuration reloads and the
detached timer lifecycle. It uses synthetic panes and a fake Kilix executable;
it does not type into a live terminal.
