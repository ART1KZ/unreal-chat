#!/usr/bin/env bash
# Terminal-behaviour tests: live hint menu, tab completion of commands and
# arguments, Esc, Ctrl-C exit semantics, Ctrl-D, interactive pickers. Runs the
# client under a pseudo-terminal via script(1) and asserts on the visible output.
set -uo pipefail
repo=$(cd "$(dirname "$0")" && pwd)
client="$repo/uarchat.py"
work=$(mktemp -d)
export HOME="$work/home" XDG_STATE_HOME="$work/state"
export UACHAT_AUTO_UPDATE=off
mkdir -p "$HOME/.config/uachat"
trap 'rm -rf "$work"' EXIT
printf 'UACHAT_PROVIDER=opencode-go\n' > "$HOME/.config/uachat/env"
pass=0
fail=0

check() { # name, haystack, needle
  if printf '%s' "$2" | grep -qF -- "$3"; then
    echo "ok   $1"
    pass=$((pass + 1))
  else
    echo "FAIL $1 (expected '$3')"
    fail=$((fail + 1))
  fi
}

strip() { sed -e 's/\x1b\[[0-9;?]*[A-Za-z]//g' -e 's/\x1b\][^\x07]*\x07//g' -e 's/\x1b[78]//g'; }

# run "sleep:text" ... — feed keystrokes with pauses so the editor is already
# reading when they arrive.
run() {
  local frags=("$@")
  { for frag in "${frags[@]}"; do sleep "${frag%%:*}"; printf '%b' "${frag#*:}"; done; } \
    | timeout 15 script -qec "$client -w $work -s tty-test --no-color" /dev/null 2>&1 | strip
}

mkdir -p "$work"
export UACHAT_NOTIFY=off
unset UACHAT_PROVIDER UNREAL_HARNESS_LLM_PROVIDER UNREAL_HARNESS_LLM_BASE_URL UNREAL_HARNESS_LLM_API_KEY UNREAL_HARNESS_LLM_MODEL UACHAT_THINKING 2>/dev/null || true

# 1. Tab completion: /th + Tab completes /thinking, menu lists commands
out=$(run "0.5:/th\t" "0.5:\x1b" "0.4:\x15/exit\n")
check "tab completes a command" "$out" "/thinking"
check "hint menu lists commands" "$out" "/thinking  pick the reasoning effort"

# 2. Enter accepts highlighted hint in autocomplete
out=$(run "0.5:/th\n" "0.5:\x1b" "0.4:\x15/exit\n")
check "Enter accepts the highlighted hint in autocomplete" "$out" "/thinking"

# 3. Interactive /provider picker with arrow navigation (starts at opencode-go, Down selects openai-codex)
out=$(run "0.5:/provider\n" "0.5:\x1b[B" "0.5:\n" "0.4:\x15/exit\n")
check "interactive provider picker moves with Down arrow and selects" "$out" "provider: openai-codex"

# 4. Interactive /model picker with live typing filter in openai-codex, then chained thinking picker
out=$(run "0.5:/model\n" "0.5:sol" "0.5:\n" "0.5:\n" "0.4:\x15/exit\n")
check "interactive model picker filters by typing and selects" "$out" "model: gpt-5.6-sol"
check "chained thinking picker confirms effort" "$out" "thinking:"

# 5. Inline model completion after space (/model gpt + Tab) in openai-codex
out=$(run "0.5:/model gpt\t" "0.5:\x1b" "0.4:\x15/exit\n")
check "tab completes a model argument inline" "$out" "/model gpt-"

# 6. Ctrl-C ladder
out=$(run "0.6:" "0.8:\x03" "0.5:/themes\n" "0.4:\x15/exit\n")
check "first Ctrl-C announces the exit hint" "$out" "Ctrl-C again to exit"
check "client survives the first Ctrl-C" "$out" "gruvbox"

out=$(run "0.6:" "0.8:\x03\x03")
check "second Ctrl-C exits" "$out" "bye"

# 7. Ctrl-D
out=$(run "0.6:" "0.4:\x04")
check "Ctrl-D exits from an empty prompt" "$out" "› "


echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
