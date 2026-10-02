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
```

In-chat commands (TAB completes commands and their arguments, ↑/↓ walk history):

| Command | Effect |
| --- | --- |
| `/new [name]` | start a fresh session, optionally named |
| `/sessions` | list recent sessions with their first prompt |
| `/resume <name>` | switch to an existing session |
| `/session` | print the current session id |
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
- Per-turn footer: `╵ in N · cached N · out N (~M tok/s) · 1.7s`; the banner shows `model · ◉ max` (thinking glyphs ○ ◔ ◑ ◒ ◕ ◉).
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
