# unreal-chat (`uachat`)

[![Release](https://img.shields.io/github/v/release/ART1KZ/unreal-chat?style=flat-square&color=blue)](https://github.com/ART1KZ/unreal-chat/releases)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Harness](https://img.shields.io/badge/harness-unreal--agent-8b5cf6?style=flat-square)](https://github.com/unreallabsai/unreal-agent)
[![RTK Compression](https://img.shields.io/badge/RTK-Token%20Compression%20(60--90%25)-ea580c?style=flat-square)](https://github.com/rtk-ai/rtk)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)

Terminal UI & interactive chat client for the [unreal-agent](https://github.com/unreallabsai/unreal-agent) harness.

`uachat` (or `unreal-chat`) is a client, not a harness: one prompt starts one `unreal-agent-runner`
process, its JSONL session records are rendered as they arrive, and the
conversation continues through a persisted session id. The harness itself ships
no interactive UI — this wraps its `codex exec`-style runner into an ergonomic,
batteries-included developer console.

## Installation

Clone the repository and run `./install.sh`:

```sh
git clone https://github.com/ART1KZ/unreal-chat.git
cd unreal-chat
./install.sh
```

### What `install.sh` does:
1. **Verifies dependencies:** checks `python3` (3.10+), `curl`, `tar`, `sha256sum`, and `git`.
2. **Installs the harness:** downloads and verifies the official `unreal-agent-runner` release binary against published `SHA256SUMS` to `/usr/local/bin`.
3. **Installs RTK:** configures [Rust Token Killer](https://github.com/rtk-ai/rtk) and `rtk-shell` to compress Bash tool command outputs by 60–90%.
4. **Symlinks the CLI:** links `uachat` and `unreal-chat` to `/usr/local/bin` and `~/.local/bin`.
5. **Windows shims (WSL):** automatically creates `uachat.cmd`, `unreal-chat.cmd`, `unreal-agent-runner.cmd`, and `uar.cmd` in your Windows `%USERPROFILE%\.local\bin`, allowing you to run `uachat` directly from Windows PowerShell or CMD.
6. **Configures environment:** creates `~/.config/uachat/env` with default provider and model settings; external credentials are not imported automatically.
7. **Secures commits:** hooks `check-secrets.sh` as the Git pre-commit scanner.

## Usage

```sh
uachat                                   # chat in the current directory
uachat -w ~/proj                         # workspace for the Bash tool
uachat -s my-chat                        # create or resume a named session
uachat -p 'Summarize this project.'      # one-shot, no chat loop
uachat --list                            # recent sessions with their first prompt
uachat --themes | --theme nord           # colour themes
uachat -t max -m deepseek-v4.1-flash     # thinking level and model overrides
uachat --color always                    # force ANSI colours (auto|always|never)
uachat --context-window 128000           # explicit limit; use your actual model limit
```

In-chat commands (TAB completes commands and their arguments, ↑/↓ walk history):

| Command | Effect |
| --- | --- |
| `/new [name]` | start a fresh session, optionally named |
| `/sessions` | list recent sessions with their first prompt |
| `/resume <name>` | switch to an existing session |
| `/session` | print the current session id |
| `/status` | show current model, thinking, context and session usage |
| `/context [tokens\|auto]` | set an explicit context limit or use catalogue metadata |
| `/provider [id]` | show or switch the provider (opencode-go, openrouter, openai, fireworks, ollama); picks a matching model automatically |
| `/model [id]` | show or switch the model (`refresh` re-reads the provider) |
| `/thinking [lvl]` | show or switch the reasoning effort (`next` cycles) |
| `/theme <name>`, `/themes` | switch theme (persisted to the config file) |
| `/copy` | copy the last answer to the clipboard (OSC 52; works over WSL/SSH) |
| `/dump` | write the transcript to `~/.local/state/uachat/transcripts` |
| `/rtk` | show RTK token-saving status and savings stats (`rtk gain`) |
| `/repair [name]` | copy a session without duplicate tool outputs, then `/resume <name>-rep` |
| `/help`, `/exit` | usage, leave |

Keys:

| Key | Effect |
| --- | --- |
| `Tab` | accept the highlighted hint |
| `Enter` | accept the highlighted hint when it differs from what you typed; otherwise send |
| `→` | accept at end of line, otherwise move the cursor |
| `↑`/`↓` | move inside the hint menu, else walk history |
| `Esc` | close the hint menu / cancel interactive picker |
| `Esc` during a turn | interrupt the agent immediately (session survives) |
| `Ctrl-C` | clear the draft; twice within 4 s leaves the client |
| `Ctrl-C` during a turn | interrupt the turn (session survives) |
| `Ctrl-A/E/U/K/W/L` | line editing (home/end/kill/word-erase/clear) |

## Interface

- Live hint menu above the input while typing `/…`: commands, then their
  arguments (models from the gateway, thinking levels, themes, sessions), with
  the highlighted entry shown inline; `Tab` accepts.
- Spinner with elapsed time, active phase and interrupt hint while the model works (`⠋ thinking (2.4s) · Esc to interrupt`, `⠋ executing Bash (3.1s)`, `⠋ streaming ~38 tok/s · 450 tok`); cleared before each event.
- Tool calls as `⏵ Bash <command>`, results as `⎿ <line>` (long output truncated with the full file path; noisy `curl`/`wget` progress bars are automatically filtered).
- Assistant message header shows the exact model name and reasoning effort: `⏺ deepseek-v4.1-flash (◉ max)` or `⏺ gpt-5.6-sol (◉ max)` with a wrapping gutter.
- Resuming a session (via `-s` or `/resume`) automatically replays the past conversation turns so you see the history immediately.
- All Bash commands executed by the agent automatically run through `rtk-shell` (Rust Token Killer, v0.50.0), cutting up to 60-90% of token consumption from command outputs (`git status`, `ls`, `grep`, `pytest`, `npm test`, etc.). Check stats with `/rtk`.
- Per-turn usage summary: `╵ in N · cached N · out N (~M tok/s) · 1.7s`; the current model/effort live in the pinned bottom status bar, not a stale startup banner.
  `UACHAT_NOTIFY=off`).
- Six 256-colour themes: `midnight`, `nord`, `gruvbox`, `neon`, `paper`, `mono`.
  Colour is on when stdout is a terminal; `--color always|never`,
  `UACHAT_COLOR=always|never` and `NO_COLOR` override that (`mono` is
  deliberately colourless — if the UI looks grey, check `/theme`).

## Providers

`/provider` switches between them; `/model` lists that provider's models. Keys
come from the environment or from the omp credential store.

| id | auth | notes |
| --- | --- | --- |
| `opencode-go` | API key | goes through `bridge.py` (needs `x-opencode-session`) |
| `openrouter` | API key | direct |
| `openai` | API key | direct |
| `openai-codex` | **ChatGPT subscription (OAuth)** | harness `openai-codex`; see below |
| `google-antigravity` | Google OAuth | experimental standalone Cloud Code bridge |
| `fireworks` | API key | direct |
| `ollama` | keyless | local server at `127.0.0.1:11434` |

### Codex / ChatGPT

The harness reads uachat's private Codex `auth.json` (mode `0600`,
`auth_mode: chatgpt`). Sign in directly with `uachat login openai-codex` or
`uachat login openai-codex --headless`. uachat refreshes its own tokens before
turns; neither OMP nor Codex CLI is required. See **Standalone authentication**
below for status/logout and optional migration commands.

## Provider setup

Defaults come from `~/.config/uachat/env` (`KEY=VALUE`, non-empty process env wins):

```dotenv
UNREAL_HARNESS_LLM_MODEL=deepseek-v4.1-flash
UNREAL_HARNESS_LLM_MAX_ATTEMPTS=3
UACHAT_THINKING=max
UACHAT_THEME=midnight
UACHAT_BRIDGE_TARGET=https://opencode.ai/zen/go/v1
UACHAT_BRIDGE_KEY=sk-...
```

With `UACHAT_BRIDGE_TARGET` + `UACHAT_BRIDGE_KEY` set, `uachat` starts
`bridge.py` on a free loopback port and points the harness at it. The bridge is
needed because OpenCode Go requires `x-opencode-session` (stable per
conversation, for routing and prompt caching) and a client-specific
User-Agent, while the harness cannot set custom headers. It forwards the
Responses API body verbatim and streams the SSE reply back.

An explicit `UNREAL_HARNESS_LLM_BASE_URL` in the environment disables the bridge.

Optional legacy migration: `extract-key.py` regenerates the env file from your omp credential store
(`~/.omp/agent/agent.db`) — re-run it after the key rotates:

```sh
python3 ~/uachat/extract-key.py
```

## Keeping the core updated

The harness binary is stock upstream; `update-core.sh` keeps it current:

```sh
uachat --update-core        # same as ./update-core.sh
./update-core.sh --check    # what is installed vs what upstream has
./update-core.sh --force    # reinstall the latest release
```

- `uachat` runs the check in the background at most once every 24 h (stamp in
  `~/.local/state/uachat/last-check`) and never blocks the session.
- Updates are verified against the release `SHA256SUMS` before `sudo install`,
  and the installed tag is recorded in `~/.local/state/uachat/core.version`.
- When something was installed, the next start prints `core: updated …`.
- Disable with `UACHAT_AUTO_UPDATE=off` in `~/.config/uachat/env`.
- Cron alternative (WSL keeps cron running while the distro is up):

```cron
17 4 * * * /home/<user>/uachat/update-core.sh --quiet
```

## Repo hygiene

- Runtime config `~/.config/uachat/env` (API key) and `~/.config/uachat/history`
  live **outside** the repository; `.gitignore` also blocks `env`, `history`,
  `__pycache__/`, logs and the state files.
- `extract-key.py` looks up the credential store via `$UACHAT_OMP_DB`,
  `~/.omp/agent/agent.db`, or `/mnt/c/Users/*/.omp/agent/agent.db`, and writes
  the key only to `~/.config/uachat/env` (mode 0600). The bridge receives the key
  through the environment, so it never shows up in `ps`.
- `check-secrets.sh` fails on credential-shaped strings; the repo ships it as a
  pre-commit hook (`.git/hooks/pre-commit` → `exec ./check-secrets.sh`).

## Troubleshooting

`responses API error invalid_request_error: Duplicate tool output for call_id: ...`
— the harness records a tool-call status twice and sends one `function_call_output`
per record; providers that validate the history reject it
([unreal-agent #11](https://github.com/unreallabsai/unreal-agent/issues/11)).

- The bridge now collapses those duplicates on the wire (the last output per call
  id wins) and appends a note to `~/.local/state/uachat/bridge.log`; set
  `UACHAT_BRIDGE_DEBUG=1` to also dump the outgoing body to
  `~/.local/state/uachat/bridge-last-request.json`.
- For sessions that already fail on resume: `/repair [name]` writes `<name>-rep`
  with the duplicate status records removed and the sequences renumbered
  (`repair-session.py` does the same from the shell), then `/resume <name>-rep`.

## Limitations (upstream, not client bugs)

- **No token streaming.** `include_partial_messages` is accepted but ignored
  ([issue #10](https://github.com/unreallabsai/unreal-agent/issues/10)), so text
  appears per completed item, not per token.
- **No mid-run steering.** The runner CLI exposes no control-input injection;
  the harness's `when_idle` control inputs are only reachable through the Go
  library. Ctrl-C cancels the whole turn instead.
- **No permission prompts.** The harness deliberately has none — approvals
  belong to the sandbox around it.

## Tests

`test.sh` runs the client against `mock_responses.py` (an SSE Responses API
stand-in): one-shot mode, a two-turn chat that resumes one session, Ctrl-C
mid-turn, and the provider-unreachable path.

`surface-test.sh` covers the non-interactive surface: `--themes`, `--list`,
completer data, and forced-colour output (it restores the config afterwards).

`tty-test.sh` drives the client through a pseudo-terminal and asserts on what a
user sees: the hint menu, Tab completion of commands and arguments, the
Ctrl-C hint/exit ladder, and Ctrl-D.

`check-secrets.sh` scans for credential-shaped strings (wired as a pre-commit hook).

```sh
./test.sh && ./surface-test.sh && ./tty-test.sh && ./check-secrets.sh
```

## Standalone authentication

No OMP or Codex CLI installation is required for these providers:

```sh
uachat login openai-codex                 # browser OAuth + localhost callback
uachat login openai-codex --headless      # device-code login, browser elsewhere
uachat auth status
uachat logout openai-codex
uachat login google-antigravity
uachat login google-antigravity --headless # manually paste redirected callback URL
uachat auth status google-antigravity
```

In chat: `/login openai-codex [--headless]`, `/login google-antigravity`,
`/auth`, `/logout [provider]`. Login does not change the selected provider;
use `/provider` afterwards. Device login may require enabling device-code
access in ChatGPT settings. Browser callbacks use ports 1455 (Codex) and
51121 (Google); if occupied, stop the other login or use headless mode.

Credentials belong to uachat, are atomically written with mode 0600, and
refresh under a process lock. Codex refresh is checked before each turn;
Antigravity refresh is also checked at each upstream request. Logout does
not delete or re-import credentials from other clients. Optional one-time
migration: `uachat login openai-codex --import-existing`.

### Antigravity (experimental)

Google OAuth application parameters are deliberately **not embedded in the
repository**. Set `UACHAT_ANTIGRAVITY_CLIENT_ID` and
`UACHAT_ANTIGRAVITY_CLIENT_SECRET` in your private `~/.config/uachat/env`
(mode 0600) or environment, using the authorized OAuth application configuration.
These are application parameters, not your account access/refresh tokens.
Never put them in commits. Windows shims generated by `install.sh` forward
these variables through WSLENV.

Select `/provider google-antigravity`. The stdlib-only local bridge translates
Responses messages/function tools to Cloud Code Assist. It uses the **daily**
endpoint and the `aicode-consumers` project fallback when project onboarding
is unavailable/ineligible, following the routing/project workaround in
`ART1KZ/omp-antigravity-pro`. Gemini Flash effort is sent as wire-model tiers
and numeric thinking budgets (1000 / 4000 / 10000). Higher effort levels map
to the high tier, not fictitious additional tiers.

Tool signatures are stored in private per-session sidecars under
`~/.local/state/uachat/antigravity/` for replay/resume. Usage/cache counts come
only from upstream. A 401 can trigger one refresh retry before any output
is sent. Quota/region/access errors are reported, not retried indefinitely.

**Limitations:** text input and function tools only; single Google account;
bundled model IDs are candidates, not a guarantee of account availability.
There is no multi-account rotation or quota dashboard yet. Offline tests
validate protocol conversion and runner tool roundtrips, but real Google
OAuth, regional access, Claude/Gemini wire compatibility and long-session
signature behavior still need live validation. Daily routing cannot guarantee
availability in any particular country.

## Multiline input

The prompt now displays a visible cursor and wraps by terminal columns,
including Cyrillic and wide characters. Large drafts scroll within the
terminal instead of growing beyond its height. Up/Down move between visual
rows in a multiline draft; on a single line they keep the history behavior
(and navigate completion suggestions when those are open).

Clipboard paste uses bracketed-paste mode in terminals that support it:
newlines are retained as text and do **not** send a message. Press Enter to
send after reviewing the paste. For a manual newline use **Alt+Enter**;
**Shift+Enter** also works when the terminal sends the extended key sequence.
Multiline drafts are preserved as one history entry. Terminals without
bracketed-paste support cannot reliably distinguish pasted Enter from typed
Enter; use a terminal with bracketed-paste support for safe multiline paste.

Offline regression tests: `python3 -m unittest -v test_editor_auth test_antigravity`.


## Bottom status bar (0.4)

Interactive TTY mode reserves the terminal's **last physical row** for current
model, reasoning effort, context occupancy, provider, session, generated/cached
token totals and last-turn duration (space permitting). It refreshes while
editing and waiting for the runner, survives resize and works with `--no-color`.
The startup header contains only version/workspace/help, not mutable settings.
Model catalogue metadata is prefetched in the background without changing your
model or blocking the editor.
Small terminals under four rows temporarily disable the reserved bar. No
alternate screen is used; ordinary scrollback remains available. Exit, errors,
Ctrl-C and SIGTERM/SIGHUP restore the terminal and its scroll region.

Context is **approximate**, marked `~`: the last reported model input plus its
output. It is NOT cumulative billing usage, and cache hits are NOT subtracted
from occupied context. The runner provides no live tokenizer/compaction API.
A percentage requires the real model limit from catalogue metadata; otherwise
`ctx ?` / `ctx ~N / ?` is displayed rather than inventing a limit.

```text
/status                 show the complete status without terminal truncation
/context 128000         example explicit limit — use your model's actual limit
/context auto           remove the override and use catalogue metadata
```

An explicit limit can also be set with `--context-window TOKENS` or
`UACHAT_CONTEXT_WINDOW` in the environment/private config. The override applies
until cleared; when changing to a model with a different limit, change it or
use `auto`. No monetary cost is estimated without reliable pricing metadata.

### Reliability and privacy

- `/model` and `/thinking` update the actual request, including after `-m`/`-t`.
  Incomplete catalogues no longer silently replace a configured model.
- Typing/pasting during a running turn is preserved as a **reviewable next
  draft**, not dropped or automatically submitted. Esc/Ctrl-C still interrupt.
- Clipboard paste is bounded at 8 MiB. Huge submissions have a bounded terminal
  preview; the full draft is sent/persisted. Visual layout stores row spans,
  not a per-character cursor map. History is bounded to 500 entries / 16 MiB.
- History/config/transcript files are private and atomically written. Concurrent
  history/config writers merge under file locks. Readline cannot overwrite the
  raw editor's JSON multiline history.
- Session replay keeps only the last five turns in memory; `/dump` streams the
  full session. Historical answers are marked `agent · replay`, not falsely
  attributed to the currently selected model.
- Model/tool text cannot execute ANSI/OSC terminal commands. Notifications and
  status-control sequences are suppressed in pipes. Duplicate response IDs and
  malformed JSON event shapes are handled defensively.
- The OpenCode bridge binds loopback on port 0, requires a random local bearer
  key, rejects unknown routes/oversized bodies and forwards streaming bytes
  without waiting for a full 8 KiB buffer. Telemetry is isolated per client.
  Credential-bearing HTTP requests refuse redirects to a different origin.
- Tests use temporary HOME/state directories and local mock services; they do
  not modify a user's credentials/config/sessions. `bash test.sh` runs the full
  regression suite, `bash tty-test.sh` tests the command UI, and
  `bash surface-test.sh` checks non-TTY behavior. Real-runner tests are skipped
  if the binary is not installed.

This hardening does **not** remove Antigravity's experimental label: live OAuth,
regional availability and provider-specific signature/wire compatibility still
require validation against an authorized account. Offline tests are not proof
of Google availability or production certification.

## Codex login, subscription usage and account rotation (0.5)

Login now uses the selected theme. WSL opens the **Windows** default browser
via PowerShell (or wslview), never the unsupported Linux gio path. Browser
launcher output is suppressed and the OAuth query is shown as a compact
clickable link in interactive terminals. If launch fails, the full manual URL
and headless instructions are shown. Appearance flags work before/after the
command; `--print-url` explicitly prints the complete browser OAuth URL.

```sh
uachat login openai-codex
uachat --color always login openai-codex --theme nord
uachat login openai-codex --headless
uachat login openai-codex --print-url
```

If port 1455 is occupied, cancel the previous login rather than killing an
unknown listener, or use headless device login. WSL browser login relies on
Windows-to-WSL localhost forwarding; device login does not require it.

### Subscription limits, not context tokens

```sh
uachat usage                           # refresh the active Codex account
uachat usage --all                     # all saved Codex accounts
uachat usage --cached                  # allow a recent 60-second cache
uachat auth accounts
uachat auth use --account EMAIL_OR_ID
uachat auth rotation on
uachat auth rotation off
uachat logout openai-codex --account EMAIL_OR_ID
uachat logout openai-codex --all
```

In chat: `/usage [--all] [--cached]`, `/accounts`, `/accounts use EMAIL_OR_ID`,
`/rotation on|off`, `/auth use --account EMAIL_OR_ID`. These management commands
currently support **Codex only**; Google usage/rotation remains unimplemented
and is reported explicitly rather than returning invented data.

Usage reads the official WHAM quota response: plan, primary/secondary window
percentages, additional quota groups, reset times, and credit balance if present.
Window labels come from server durations (not hardcoded 5h/7d assumptions).
The footer can show remaining subscription quota separately from `ctx`.
An HTTP 403/network error is **unknown usage**, not 100% used. `/usage` returns
nonzero if it could not obtain quota data for any requested account.

### Multiple accounts and safe rotation

Repeated login adds/updates an account rather than replacing all saved
credentials. The initial existing **uachat** auth file is migrated once;
external OMP/Codex stores are never consulted implicitly. Account credentials
are private files under `~/.config/uachat/codex/accounts/`; `accounts.json`
contains selection state and quota cache. All are mode 0600.

Autorotation defaults **on**. Before launching a Codex turn, the client checks
cached/fresh applicable quota and switches a depleted account to an available
saved sibling. Selection is sticky per session. Expired quota windows invalidate
the cache so new limits/reset allowances are read. An explicit backend
`allowed: true` is honored even when a displayed window reaches 100%.
No quota resets or credits are automatically redeemed/purchased. Normal model
requests can still consume credits according to backend policy; this feature
is not a spending cap.

There must be at least two authorized accounts to switch. If all known accounts
are exhausted, the turn is not started. Turning rotation off pins selection
rather than trying other accounts. Generic HTTP 429, country/security 403 and
transient network errors do not themselves trigger rotation.

If a quota error occurs **during** a turn, the account is marked for a fresh
quota check on the next request. The already-running agent prompt is **not
replayed automatically**, because that could execute Bash/tools twice. Continue
or retry explicitly after checking what the previous turn already did.

Each runner gets its own temporary 0600 auth snapshot; changing the active
account cannot overwrite a different running turn's credentials. Same-account
turns/refreshes are serialized with account leases. A runner's token refresh is
saved back to the matching account, and the snapshot is deleted on exit. Orphan
snapshots are cleaned after their owning PID is gone. Independent third-party
clients sharing manually imported refresh tokens are outside these locks;
prefer a native login rather than concurrent refresh from an imported source.

**Validation:** quota endpoint/schema has been checked with a real saved token
(read-only HTTP 200, no token refresh or credential changes). WSL PowerShell
interop and local OAuth callback/PKCE are tested. Rotation and no-replay behavior
have regression tests, including runner integration. A complete fresh browser
OAuth flow still requires the user's consent, and live cross-account replay of
provider-encrypted history has not been certified. Do not claim Google support
or universal provider compatibility from these tests.


### Auth/usage hardening (0.5.1)

```sh
uachat doctor                      # same as auth doctor (Codex)
uachat auth doctor google-antigravity
```

In chat: `/doctor [provider]`. Diagnostics are **read-only**: launcher presence,
callback-port availability, runner presence, own auth files, local access expiry,
refresh-token presence and file permissions. No browser/network/refresh is
triggered; proxy values and token contents are hidden. Launcher detection and
local expiry do not prove server-side authentication or connectivity.

- Login validates new credentials in memory; a failed pool registration does not
  overwrite the active compatibility auth file. Refresh returning a different
  account/user, malformed token fields or API-key credentials is rejected before
  writing over valid stored credentials.
- Busy account leases no longer hang another client indefinitely. Rotation can
  select an idle saved sibling; otherwise preparation fails clearly. Account
  use/register/logout waits at most five seconds. `usage --cached` can return
  recent cached data while a turn owns that account; a forced network check does
  not race a running account refresh.
- A per-session client lock prevents two uachat processes from concurrently
  modifying the same conversation, even with different accounts/providers. This
  does not lock independent direct invocations of upstream unreal-agent-runner.
- Preflight failure or cancellation preserves an editable prompt in the raw
  editor. No failed/already-started agent turn is automatically replayed.
- The footer monitors the private quota cache's file version: another client's
  usage refresh or account selection is reflected without typing or HTTP polling.
  Status belongs to this session, not blindly to the global active account.
  Quota warnings are scoped to the affected model and clear after a successful
  authoritative quota refresh. Account listing shows safe refresh-failure hints.

Regression tests include separate-process lock holders, PTY draft restoration,
idle footer updates, cross-account refresh rejection, failed registration and
read-only diagnostics. The full offline/mock suite now has 72 tests, plus the
11 command-UI checks.
