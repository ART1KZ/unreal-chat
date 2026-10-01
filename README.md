# uachat

Terminal chat client for the [unreal-agent](https://github.com/unreallabsai/unreal-agent) harness.

`uachat` is a client, not a harness: one prompt starts one `unreal-agent-runner`
process, its JSONL session records are rendered as they arrive, and the
conversation continues through a persisted session id. The harness itself ships
no interactive UI — this wraps its `codex exec`-style runner.

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
| `/model [id]` | show or switch the model (`refresh` re-reads the gateway) |
| `/thinking [lvl]` | show or switch the reasoning effort (`next` cycles) |
| `/theme <name>`, `/themes` | switch theme (persisted to the config file) |
| `/copy` | copy the last answer to the clipboard (OSC 52; works over WSL/SSH) |
| `/dump` | write the transcript to `~/.local/state/uachat/transcripts` |
| `/help`, `/exit` | usage, leave |

Keys:

| Key | Effect |
| --- | --- |
| `Tab` | accept the highlighted hint |
| `↑`/`↓` | move inside the hint menu, else walk history |
| `Esc` | close the hint menu |
| `Ctrl-C` | clear the draft; twice within 4 s leaves the client |
| `Ctrl-C` during a turn | interrupt the turn (the session survives) |
| `Ctrl-D` | leave |
| `Ctrl-A/E/U/K/W/L` | line editing (home/end/kill/word-erase/clear) |

## Interface

- Live hint menu above the input while typing `/…`: commands, then their
  arguments (models from the gateway, thinking levels, themes, sessions), with
  the highlighted entry shown inline; `Tab` accepts.
- Spinner with elapsed time while the model works; cleared before each event.
- Tool calls as `⏵ Bash <command>`, results as `⎿ <line>` (long output truncated
  with the full file path), assistant text under a `⏺ agent` marker with a
  wrapping gutter.
- Per-turn footer: `╵ in N · cached N · out N · 1.7s`; the banner shows
  `model · ◉ max` (thinking glyphs ○ ◔ ◑ ◒ ◕ ◉).
- Desktop toast on turn completion/failure (OSC 9 + `notify-send`; disable with
  `UACHAT_NOTIFY=off`).
- Six 256-colour themes: `midnight`, `nord`, `gruvbox`, `neon`, `paper`, `mono`.
  Colour is on when stdout is a terminal; `--color always|never`,
  `UACHAT_COLOR=always|never` and `NO_COLOR` override that (`mono` is
  deliberately colourless — if the UI looks grey, check `/theme`).

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

`extract-key.py` regenerates the env file from your omp credential store
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
