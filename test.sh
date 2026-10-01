#!/usr/bin/env bash
set -u
echo "PATH_HEAD=$(echo "$PATH" | cut -d: -f1-3)"
echo "which_uachat=$(command -v uachat)"
export UNREAL_HARNESS_LLM_PROVIDER=ollama
export UNREAL_HARNESS_LLM_BASE_URL=http://127.0.0.1:11434/v1
export UNREAL_HARNESS_LLM_MODEL=mock
export UNREAL_HARNESS_LLM_MAX_ATTEMPTS=1
client=/usr/local/bin/uachat
work=/tmp/uachat-ws
mkdir -p "$work"
cd "$HOME/uachat"
python3 mock_responses.py > "$work/mock.log" 2>&1 &
mock_pid=$!
sleep 1

echo "=== 1. one-shot (-p) ==="
rm -f "$HOME/.local/state/unreal-agent/sessions/oneshot.session.jsonl"
timeout 60 "$client" -p "run echo one" -w "$work" -s oneshot --no-color
echo "exit=$?"

echo
echo "=== 2. chat: two turns on one session ==="
printf 'first turn\nturn two\n/exit\n' | timeout 60 "$client" -w "$work" -s chat-e2e --no-color
echo "session_lines=$(wc -l < "$HOME/.local/state/unreal-agent/sessions/chat-e2e.session.jsonl")"

echo
echo "=== 3. Ctrl-C mid-turn ==="
printf 'slow turn please\n/exit\n' | timeout -s INT 3 "$client" -w "$work" -s chat-int --no-color
echo "exit=$?"

kill "$mock_pid" 2>/dev/null
wait "$mock_pid" 2>/dev/null

echo
echo "=== 4. provider unreachable: client must survive ==="
printf 'unreachable turn\n/exit\n' | timeout 60 "$client" -w "$work" -s chat-err --no-color
echo "exit=$?"
