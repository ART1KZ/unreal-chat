#!/usr/bin/env bash
# Terminal-behaviour tests: live hint menu, tab completion of commands and
# arguments, Esc, Ctrl-C exit semantics, Ctrl-D. Runs the client under a
# pseudo-terminal via script(1) and asserts on the visible output.
set -uo pipefail
client=/usr/local/bin/uachat
work=/tmp/tty-test
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

strip() { sed -e 's/\x1b\[[0-9;?]*[A-Za-z]//g' -e 's/\x1b\][^\x07]*\x07//g'; }

# run "sleep:text" ... — feed keystrokes with pauses so the editor is already
# reading when they arrive (a Ctrl-C byte sent before raw mode becomes SIGINT).
run() {
  local frags=("$@")
  { for frag in "${frags[@]}"; do sleep "${frag%%:*}"; printf '%b' "${frag#*:}"; done; } \
    | script -qec "$client -w $work -s tty-test --no-color" /dev/null 2>&1 | strip
}

mkdir -p "$work"
export UACHAT_NOTIFY=off
cp "$HOME/.config/uachat/env" /tmp/tty-env.backup 2>/dev/null || true

out=$(run "0.6:/th\t\n" "0.4:/exit\n")
check "tab completes a command and Enter runs it" "$out" "current thinking:"
check "hint menu lists commands" "$out" "/thinking  pick the reasoning effort"

out=$(run "0.6:/th\n" "0.7:\n" "0.4:/exit\n")
check "Enter accepts the highlighted hint instead of sending" "$out" "current thinking:"

out=$(run "0.6:/model dee\n" "0.7:\n" "0.4:/exit\n")
check "Enter accepts a highlighted model argument" "$out" "model: deepseek-"

out=$(run "0.6:/provider\n" "0.4:/exit\n")
check "provider picker lists the providers" "$out" "openrouter"

out=$(run "0.7:/model " "0.6:\x1b[B\x1b[B" "0.6:\n" "0.6:\n" "0.4:/exit\n")
check "arrow keys move inside the model menu" "$out" "model: deepseek-v4-flash-vision-exp"

out=$(run "0.6:/model \n" "0.4:/exit\n")
check "argument menu lists gateway models" "$out" "deepseek-v4.1-flash"

out=$(run "0.6:/model dee\t\n" "0.4:/exit\n")
check "tab completes a model argument" "$out" "model: deepseek-"

out=$(run "0.8:" "1.2:\x03" "0.6:/themes\n" "0.4:/exit\n")
check "first Ctrl-C announces the exit hint" "$out" "Ctrl-C again to exit"
check "client survives the first Ctrl-C" "$out" "gruvbox"

out=$(run "0.8:" "1.2:\x03\x03")
check "second Ctrl-C exits" "$out" "bye"

out=$(run "0.8:" "0.4:\x04")
check "Ctrl-D exits from an empty prompt" "$out" "› "

cp /tmp/tty-env.backup "$HOME/.config/uachat/env" 2>/dev/null && chmod 600 "$HOME/.config/uachat/env"
rm -f /tmp/tty-env.backup
rm -rf "$work" /home/kai/.local/state/unreal-agent/sessions/tty-test.session.jsonl

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
