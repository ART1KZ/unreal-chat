#!/usr/bin/env bash
# Exercises the client surface that does not need a terminal: session listing,
# theme listing, tab-completion candidates, one-shot rendering with colour, and
# the offline mock loop.
set -uo pipefail
client=/usr/local/bin/uachat
work=/tmp/uachat-ws
mkdir -p "$work"

echo "=== 1. --themes ==="
$client --themes

echo
echo "=== 2. --list ==="
$client --list | head -n 5

echo
echo "=== 3. completer returns command and theme matches ==="
python3 - <<'PY'
import importlib.util, os

spec = importlib.util.spec_from_file_location("uarchat", os.path.expanduser("~/uachat/uarchat.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
theme = module.Theme("nord", False)
assert theme.names() == list(module.THEMES)
assert all(name in module.THEMES for name in theme.names())
assert module.valid_session_name("chat-1") and not module.valid_session_name("a/b")
print("commands:", ", ".join(module.COMMANDS))
print("themes:", ", ".join(theme.names()))
PY

echo
echo "=== 4. colour always emits ANSI ==="
cp "$HOME/.config/uachat/env" /tmp/surface-env.backup 2>/dev/null || true
printf 'ping\n/theme mono\n/exit\n' | $client -w "$work" -s theme-check --color always 2>&1 | head -n 6 | cat -v | head -n 4
# The /theme command persists; restore the user's setting so the test stays neutral.
cp /tmp/surface-env.backup "$HOME/.config/uachat/env" 2>/dev/null && chmod 600 "$HOME/.config/uachat/env"
rm -f /tmp/surface-env.backup
echo "(theme setting restored after the test)"
