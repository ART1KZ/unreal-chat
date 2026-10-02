# AGENTS.md — development context for unreal-chat (`uachat`)

Handoff document for an AI coding agent continuing work on this project.
Read together with `README.md` (user docs) and the git history.

## 1. What this project is

`uachat` / `unreal-chat` is a terminal client for the **unreal-agent** harness
(`unreallabsai/unreal-agent`, a Go agent runner, MIT). The harness is CLI-only:
one `unreal-agent-runner` process per prompt, a JSON request on stdin, JSONL
events on stdout, no interactive UI.

This repo is a *client*, not a fork. Core design rule: **one prompt = one
`unreal-agent-runner` subprocess**; conversation continuity comes from a
persisted `session_id`. Everything is Python 3 **stdlib only** (no third-party
dependencies; the modules `editor`, `models`, `providers` are loaded from the
repo directory).

## 2. Where things live

| What | Where |
| --- | --- |
| Upstream harness | github.com/unreallabsai/unreal-agent (Go, MIT, `unreal-agent-runner`) |
| This repo | github.com/ART1KZ/unreal-chat (public, branch `main`) |
| Working copy | WSL2 Ubuntu: `/home/kai/uachat` |
| CLI entry point | `/usr/local/bin/uachat` and `/usr/local/bin/unreal-chat` — symlinks to `uarchat.py`; also in `~/.local/bin/` |
| Runner binary | `/usr/local/bin/unreal-agent-runner` (v0.2.0), stamp `~/.local/state/uachat/core.version` |
| RTK | `/usr/local/bin/rtk` (v0.50.0) + wrapper `/usr/local/bin/rtk-shell` |
| Windows shims | `C:\Users\kai\.local\bin\{uachat,unreal-chat,uar,unreal-agent-runner}.cmd` → `wsl.exe -d Ubuntu -- …` |
| Config (secrets) | `~/.config/uachat/env` (0600), `~/.config/uachat/codex/auth.json` |
| Client state | `~/.local/state/uachat/` — history, transcripts, `core.version`, `pending-note`, `last-check` |
| Harness sessions | `~/.local/state/unreal-agent/sessions/<id>.session.jsonl` |
| Git auth | repo-local credential helper pulling a token from the Windows gh keyring; `git push` works from WSL |
| `gh` CLI | **not installed in WSL** — use `cmd.exe /c gh …` or `powershell.exe -NoProfile -Command gh …` (authenticated as ART1KZ) |

Environment: Windows 10 x64 host, WSL2 Ubuntu, Python 3.14.4. Client version
string: `uachat 0.5.1` (`VERSION` in `uarchat.py`).

## 3. Architecture / data flow

```
uachat (uarchat.py)
 ├─ resolve config (~/.config/uachat/env), provider spec, model catalogue
 ├─ pick session id (~/.local/state/unreal-agent/sessions/<id>.session.jsonl)
 ├─ run_turn():
 │   Popen(["unreal-agent-runner", "-workspace", workspace],
 │         env += { SHELL: /usr/local/bin/rtk-shell })
 │   ├─ stdin  ← one JSON request (see §6)
 │   ├─ stdout → JSONL events → Renderer.handle() → terminal
 │   └─ InterruptWatcher thread: Esc → SIGTERM process group (Ctrl-C → 130)
 ├─ session replay: parse <id>.session.jsonl → restored turns
 └─ bridge.py (HTTP proxy) ← only for providers with "bridge": true (opencode-go)
```

- **Renderer** consumes event kinds: `model_response` (items `message` /
  `reasoning` / `tool_call`, plus `Usage`), `tool_call_status`
  (running/completed with output), error records. Assistant text is printed
  under `⏺ <model> (<glyph> <thinking>)`; tool calls as `⏵ Bash <cmd>`; results
  as `⎿ …`; footer `╵ in · cached · out · tok/s · time`.
- **Spinner** polls `read_stream_state()` (live stream telemetry) and shows
  `thinking (N s)` / `executing Bash (N s)` / `streaming ~N tok/s · N tok`,
  with `Esc to interrupt`.
- **RTK wrapper**: the harness runs the Bash tool via `$SHELL -c "<cmd>"`;
  `rtk-shell` rewrites the command through `rtk rewrite` when a rule matches
  (60–90 % token savings), otherwise runs it unchanged. Disable with
  `UACHAT_RTK=off|0|false|no`. Stats: `rtk gain` or in-chat `/rtk`.
- **Bridge** (`bridge.py`, used by `opencode-go`): local HTTP proxy that adds
  the `x-opencode-session` header, collapses duplicate tool outputs, and tracks
  live stream stats. Configured with `UACHAT_BRIDGE_TARGET` / `UACHAT_BRIDGE_KEY`;
  the runner sees base URL = bridge, API key = literal `bridge`.

## 4. File map

| File | Role |
| --- | --- |
| `uarchat.py` (~1690 lines) | main client: CLI parsing, config, banner/theme, Renderer, Spinner, REPL loop, slash commands, session replay, `run_turn` |
| `editor.py` (~850 lines) | raw-mode line editor: hint/autocomplete menu, history, interactive `pick()` (arrow keys, live filter) |
| `models.py` | model catalogue fetch/cache per provider |
| `providers.py` | provider table (harness kind, base URL, keys, auth hooks, model fetchers) |
| `codex_auth.py` | ChatGPT/Codex OAuth flow + token refresh (`openai-codex`) |
| `bridge.py` | Responses proxy for opencode-go (session header, dedup, stream stats) |
| `rtk-shell` | `$SHELL` wrapper rewriting Bash tool commands through RTK |
| `extract-key.py` | import provider keys from the omp store (`~/.omp/agent/agent.db`) via temp copy |
| `repair-session.py` | `/repair`: copy a session dropping duplicate tool outputs |
| `install.sh` | idempotent installer: deps check, core binary, RTK, symlinks, Windows shims, config, hooks |
| `update-core.sh` | update `unreal-agent-runner` from GitHub releases (SHA256SUMS verified; `--check`, `--force`, `--quiet`) |
| `check-secrets.sh` | secret scanner; wired as the git pre-commit hook |
| `test.sh` / `surface-test.sh` / `tty-test.sh` | offline mock tests / non-TTY surface / PTY behaviour (11 checks via `script(1)`) |
| `mock_responses.py` | fake stream responder for offline tests |

## 5. Client feature inventory

- CLI flags: `-p/--prompt`, `-w/--workspace`, `-s/--session`, `-m/--model`,
  `-t/--thinking {low,medium,high,xhigh,max}`, `--theme`, `--themes`,
  `--version`, `--list`, `--provider`, `--binary`, `--update-core`,
  `--color {auto,always,never}`, `--no-color`.
- Slash commands: `/help`, `/new [name]`, `/sessions`, `/resume <name>`,
  `/session`, `/provider [id]`, `/model [id]`, `/thinking [lvl]`, `/themes`,
  `/theme <name>`, `/copy` (OSC 52), `/dump`, `/rtk`, `/repair [name]`,
  `/exit`, `/quit`.
- Interactive pickers (no-argument forms): `/provider`, `/model` (live filter;
  chains into thinking picker), `/thinking`, `/theme`, `/sessions`/`/resume`.
- Session lifecycle: resuming (`-s`, `/resume`, `/sessions`) clears screen +
  scrollback (`\x1b[3J\x1b[H\x1b[2J`), reprints the banner, replays up to 5
  previous turns with model/effort headers, and replaces the internal
  `transcript` (used by `/copy`, `/dump`). `/new` clears everything.
- Turn control: `Esc` aborts the running turn (process-group SIGTERM, session
  survives); `Ctrl-C` interrupts; twice at the prompt exits; spinner shows the
  active phase.
- Config keys in `~/.config/uachat/env`: `UACHAT_PROVIDER`, `UACHAT_THINKING`,
  `UACHAT_THEME`, `UACHAT_NOTIFY`, `UACHAT_AUTO_UPDATE`, `UACHAT_COLOR`,
  `UACHAT_RTK`, `UACHAT_BRIDGE_TARGET`, `UACHAT_BRIDGE_KEY`, `UACHAT_OMP_DB`,
  `UNREAL_HARNESS_LLM_MODEL`, `UNREAL_HARNESS_LLM_MAX_ATTEMPTS`.

## 6. Harness contract (what the client must speak)

Request JSON written to the runner's stdin (from `unreal-agent-runner -h`):
`messages[]` (`{role, content, message_id?}`) or `prompt`; `model`;
`max_attempts` (default 5); `system_prompt`; `thinking_level`
(`low|medium|high|xhigh|max`, default `high`); `session_id`;
`disallowed_tools`; `extra_allowed_tools` / `include_partial_messages`
(accepted, ignored). Runner read the request from stdin, a positional JSON
arg, or `-p`.

Runner options: `-workspace`, `-session-directory`, `-log-directory`,
`-tool-heartbeat-interval`, `-p`. Env: `SHELL` (used for the Bash tool),
`UNREAL_HARNESS_LLM_PROVIDER` (`openai|openai-codex|openrouter|fireworks|ollama`),
`UNREAL_HARNESS_LLM_MODEL`, `UNREAL_HARNESS_LLM_API_KEY`,
`UNREAL_HARNESS_LLM_BASE_URL`, `UNREAL_HARNESS_LLM_MAX_ATTEMPTS`.

Session JSONL on disk: header `{"type":"session","data":{…}}` then records
wrapped as `{"type":"item","data":{"Item":{…}}}`. `Item.Kind`:
`input` (user prompt: `Data.Kind=="external"`, `Data.Payload.Text`),
`model_response` (`Data.Response.Output[]` items of type `message`/`reasoning`/
`tool_call`, `Data.Response.Usage`), `tool_call_status`, `turn`.

## 7. Providers

| id | harness | auth | notes |
| --- | --- | --- | --- |
| `opencode-go` | openai | API key from own config/env | goes through `bridge.py` |
| `openai-codex` | openai-codex | ChatGPT OAuth (codex_auth.py) | token at `~/.config/uachat/codex/auth.json` (0600) |
| `openrouter` | openrouter | API key | direct |
| `openai`, `fireworks` | openai/fireworks | API key | key not configured on this machine |
| `ollama` | ollama | keyless | local server at 127.0.0.1:11434 |

## 8. Development workflow

1. Edit files **inside WSL** (`/home/kai/uachat`). If a file is authored from
   the Windows side, copy it with CR stripping:
   `tr -d '\r' < /mnt/c/tmp/x.py > /home/kai/uachat/x.py`.
2. There is nothing to build — the CLI is run from the repo via symlink. Make
   sure new executables are `chmod +x`.
3. Smoke: `uachat --version`, `uachat -w /tmp -p 'run git status and report the branch' --no-color`.
4. Tests, from the repo dir: `bash tty-test.sh` (PTY, 11 assertions),
   `bash test.sh`, `bash surface-test.sh`, `./check-secrets.sh`
   (expect `secrets: clean (N tracked files)`).
5. Commit style: scoped prefixes (`client:`, `providers:`, `docs:`, `fix:`),
   imperative subject. The pre-commit hook runs `check-secrets.sh`.
6. Push: `git push origin main`. Releases: `v0.3.0` exists; create new ones
   with the Windows `gh` (from `cmd.exe`/`powershell.exe`), e.g.
   `gh release create vX.Y.Z --title … --notes-file …`.

## 9. Gotchas (learned the hard way)

- **CRLF**: repo files must stay LF. Editing `uarchat.py` from Windows without
  stripping `\r` breaks Python parsing; always `tr -d '\r'`.
- **Secrets** never enter the repo; `check-secrets.sh` enforces it. Keys live
  in `~/.config/uachat/env`, codex tokens in `~/.config/uachat/codex/`.
- `/usr/local/bin` writes need `sudo`; `install.sh` / `update-core.sh` fall
  back to `~/.local/bin` when passwordless sudo is unavailable.
- **Windows shims** forward a whitelist of env vars via `WSLENV` (see
  `install.sh`); new client env vars must be added there to survive the
  Windows → WSL hop.
- `clear_screen()` and friends are guarded by `sys.stdout.isatty()` — keep
  escape-sequence output behind such checks so non-TTY tests stay clean.
- `rtk rewrite` prints nothing when no rule matches → `rtk-shell` must fall
  back to plain `/bin/bash -c "$cmd"`.
- Session files can be tens of MB; `replay_session` streams lines and prints
  only the last `max_turns` (default 5) turns, truncating each answer to 8
  lines. Keep it streaming-friendly.
- The client is read once per process launch; edits apply on the next start.
- `gh` is absent inside WSL; use the Windows binary.

## 10. Candidate next steps (none started)

- `replay_session`: reuse `textwrap`/Renderer so replayed answers wrap to the
  terminal width instead of being untruncated long lines.
- `/export`: write a Markdown transcript (the `transcript` list already
  exists) next to `/dump`.
- Unit tests for the session JSONL parser with a fixture file (currently only
  covered indirectly by PTY tests).
- Extend the Windows shim env allowlist and document `WSLENV` behaviour.
- Configurable per-workspace default session naming.

## 11. Standalone auth and editor follow-up

- `native_auth.py`: native Codex browser PKCE/device login, locked refresh,
  own credentials, status/logout; explicit `--import-existing` migration.
  `codex_auth.py` now discovers **only uachat's auth file** by default.
- `antigravity.py`: experimental independent Google OAuth + Responses/Cloud
  Code bridge, daily routing, consumer-project fallback, numeric Flash budgets,
  private signature sidecars. No OMP runtime/store dependency. Text/function
  tools only; no rotation yet. Real provider OAuth and regional access have
  NOT been live-tested. See README limitations before calling it production.
- `editor.py`: visible cursor, wrapped multiline viewport, bracketed paste,
  Alt+Enter/extended Shift+Enter, visual-row navigation, JSON-encoded history
  entries (legacy plain lines still load). Terminal bracketed-paste support
  is required to distinguish a multiline paste from individual Enter keys.
- `test_editor_auth.py` / `test_antigravity.py`: offline regression contracts.

- API key resolution and install no longer auto-read OMP stores. Legacy import
  helpers remain explicitly opt-in; normal startup uses only uachat config.

- Google OAuth application ID/secret are private config, NOT bundled constants:
  `UACHAT_ANTIGRAVITY_CLIENT_ID` / `UACHAT_ANTIGRAVITY_CLIENT_SECRET`. Both
  generated Windows shims forward them. Secret scanner also checks Google OAuth.


## 12. 0.4 client hardening / pinned footer

- `terminal_ui.py`: safe text, JSON event shape normalization, display-column
  wrapping, SessionMetrics, TerminalFooter. Uses a protected scroll region to
  reserve the last physical terminal row; always restores full region on exit.
  Tiny (<4 row) terminals disable the bar. No UI thread/alternate screen.
- Footer reads current mutable request state; CLI flags no longer override later
  slash commands. Startup does not auto-replace models using stale catalogues.
  Catalogue metadata is prefetched in background; idle redraw picks it up.
- Context = last model input + output, APPROXIMATE (`~`), never cumulative usage
  and never minus cache. Denominator from model metadata or explicit
  `--context-window` / `UACHAT_CONTEXT_WINDOW` / `/context`. Unknown limit stays
  unknown. `/status` prints untruncated status; `/context auto` clears override.
- `configio.py`: shared private atomic writes and process locks. Editor history
  merges concurrent writers, capped 500 entries / 16 MiB; readline is disabled
  when the raw editor owns history. Paste cap 8 MiB, bounded submit preview,
  sparse visual row spans (no per-character dict), idle resize redraw.
- InterruptWatcher preserves typed/pasted text as the next editable draft,
  never auto-submits it. Raw INPUT preserves original output newline processing.
  SIGTERM/SIGHUP unwind the editor/runner/footer through ShutdownRequested.
- Replay uses a bounded deque; full /dump streams the disk session into a 0600
  atomic Markdown file. Replay model/effort are not inferred from current config.
- OpenCode bridge uses port-0 handshake and random local bearer credential,
  route/body validation and read1() forwarding. Stream telemetry is per client.
- `secure_http.py`: bearer/POST redirects across origins are rejected in all
  provider transports; private debug dumps are atomic. Auth responses must
  contain usable unexpired tokens; malformed refresh leaves old file intact.
- Test entrypoints now isolated, never copy/edit real user's config. Full suite:
  `bash test.sh` (39 tests including real PTYs and runner/mock roundtrips),
  `bash tty-test.sh`, `bash surface-test.sh`, `./check-secrets.sh`.
- Antigravity remains EXPERIMENTAL; do not claim live auth/regional compatibility
  is verified or use hardening tests as production certification.

## 13. 0.5 Codex auth UX / quotas / account pool

- `auth_ui.py`: themed login/usage UI and quiet browser opening. WSL routes to
  powershell.exe (encoded command, URL passed as base64 DATA, no shell) or
  wslview; no gio fallback. Short OSC8 link on TTY, full manual URL on failure,
  explicit `--print-url`. Native auth accepts --color/--no-color/--theme and
  common appearance flags work before the auth command too.
- `codex_pool.py`: own multi-account files + 0600 accounts.json, one-time own
  legacy-file migration, selection/account use/logout, session stickiness,
  between-turn rotation (default on), read-only WHAM usage/credits/reset display.
  Endpoint https://chatgpt.com/backend-api/wham/usage; Auth bearer + account ID.
  Durations/reset timestamps from server. Cache 60s, invalidated by window reset.
  Additional groups affect selection only when matching the current model.
  Explicit backend allowed=True wins over percentage-based blocking.
- Each Codex runner uses an independent temporary auth snapshot under an account
  lease; concurrent same-account refreshes/turns serialize. Save runner token
  refresh back only if account/user identity match. Cleanup snapshots, including
  orphan owner PIDs. The compatibility auth.json is not a live shared runner
  credential. Default turn auth setup no longer refreshes that shared file.
- Commands: CLI `usage`, `auth accounts`, `auth use --account`,
  `auth rotation on|off`, logout --account/--all; REPL /usage, /accounts [use ID],
  /rotation. Google quota/rotation commands explicitly report unsupported.
- Never replay a failed turn automatically. Preserve original model error
  instead of overwriting it with "runner exited" so typed quota errors can mark
  a cooldown/fresh check. Generic 429/403 do not mark quota exhaustion.
- Browser tests use ephemeral ports, NEVER occupy the user's real port 1455.
  WSL interop was actually probed; real WHAM read-only GET returned HTTP 200 and
  valid schema. No credentials mutated during that probe. Fresh browser OAuth
  consent and live cross-account encrypted-history compatibility remain to be
  verified by an authorized user. Don't overclaim those.
- `test_auth_pool.py` adds auth/usage/rotation/no-replay isolation coverage.
  `bash test.sh` now runs 58 tests plus secret scan; tty-test remains 11 checks.


## 14. 0.5.1 auth concurrency / diagnostics

- configio.file_lock now supports optional timeout and LockBusyError. Account
  turns try leases nonblocking, rotate to idle siblings; mutations wait <=5s.
  Recent quota cache is readable without a turn lease. No busy-account hang.
- execute_turn owns a per-session `.client.lock` across preparation and runner;
  all providers share this guard. Direct upstream runner invocations do not.
- native_auth.build_payload validates without writes; native login registers the
  result directly. Failure does not overwrite canonical active credentials.
  Account/user refresh mismatch and invalid token types/API keys rejected before
  writing. Identity extraction shared with codex_auth/pool.
- Preflight errors/Ctrl-C restore editable prompt only if renderer.was_started
  is false; never silently replay completed/started tools.
- Footer watches index inode/mtime/size and session/model signature; reads cache
  only, no migration/locks/HTTP in render callback. Typed quota flags scoped by
  model, cleared on authoritative fresh availability. Refresh failure metadata
  contains safe status/class only, never exception bodies/token values.
- diagnostics.py exposes doctor/auth doctor,/doctor: readonly local checks. No
  browser/refresh/HTTP, no secret/proxy values. Availability is not auth proof.
- test_auth_hardening.py: real separate-process locks, guard/no-launch, failed
  registration preserves active file, refresh identity/type rejection, PTY
  draft restoration/footer update, diagnostic read-only invariants. Full suite
  72 tests plus 11 tty checks. User's actual credentials/config were not mutated
  by diagnostic execution.
