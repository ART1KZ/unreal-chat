#!/usr/bin/env bash
# uachat installation script for Linux / WSL
# Installs unreal-agent-runner, rtk, rtk-shell, uachat symlinks, Windows shims, and config.
set -euo pipefail

cd "$(dirname "$0")"
REPO_DIR="$PWD"

echo "=== 1. Checking dependencies ==="
for cmd in python3 curl tar sha256sum git; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Error: required command '$cmd' is not installed." >&2
    exit 1
  fi
done
py_ver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "  python3 ($py_ver), curl, tar, sha256sum, git: OK"

echo "=== 2. Installing / updating unreal-agent-runner core ==="
if [ -x "./update-core.sh" ]; then
  ./update-core.sh
else
  echo "Error: update-core.sh not found." >&2
  exit 1
fi

echo "=== 2b. Installing live inbox adapter ==="
if [ "${UACHAT_LIVE_BUILD:-on}" != off ]; then
  "$REPO_DIR/build-live.sh"
else
  echo "  Live adapter build skipped; stock one-shot/draft mode remains available."
fi

echo "=== 3. Setting up RTK (Rust Token Killer) and rtk-shell ==="
if ! command -v rtk >/dev/null 2>&1; then
  echo "  Installing rtk..."
  curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh | sh || true
  if [ -x "$HOME/.local/bin/rtk" ]; then
    sudo ln -sf "$HOME/.local/bin/rtk" /usr/local/bin/rtk 2>/dev/null || true
  fi
fi
if command -v rtk >/dev/null 2>&1 || [ -x "/usr/local/bin/rtk" ] || [ -x "$HOME/.local/bin/rtk" ]; then
  echo "  rtk: $(rtk --version 2>/dev/null || echo 'installed')"
  sudo cp -f "$REPO_DIR/rtk-shell" /usr/local/bin/rtk-shell 2>/dev/null || cp -f "$REPO_DIR/rtk-shell" "$HOME/.local/bin/rtk-shell"
  sudo chmod +x /usr/local/bin/rtk-shell 2>/dev/null || chmod +x "$HOME/.local/bin/rtk-shell"
  echo "  rtk-shell ready for agent bash commands."
else
  echo "  Note: rtk could not be installed automatically; continuing without RTK."
fi

echo "=== 4. Symlinking uachat CLI ==="
chmod +x "$REPO_DIR/uarchat.py" "$REPO_DIR/bridge.py" "$REPO_DIR/extract-key.py" "$REPO_DIR/repair-session.py"
sudo ln -sf "$REPO_DIR/uarchat.py" /usr/local/bin/uachat 2>/dev/null || true
sudo ln -sf "$REPO_DIR/uarchat.py" /usr/local/bin/unreal-chat 2>/dev/null || true
mkdir -p "$HOME/.local/bin"
ln -sf "$REPO_DIR/uarchat.py" "$HOME/.local/bin/uachat"
ln -sf "$REPO_DIR/uarchat.py" "$HOME/.local/bin/unreal-chat"
echo "  uachat & unreal-chat symlinked to /usr/local/bin and ~/.local/bin"

echo "=== 5. Setting up Windows shims (if WSL) ==="
win_user=""
if [ -d "/mnt/c/Users/$USER" ] && [ -w "/mnt/c/Users/$USER" ]; then
  win_user="/mnt/c/Users/$USER"
elif command -v powershell.exe >/dev/null 2>&1; then
  prof=$(powershell.exe -NoProfile -Command 'Write-Host -NoNewline $env:USERPROFILE' 2>/dev/null | tr -d "\r" || true)
  if [ -n "$prof" ]; then
    candidate=$(wslpath "$prof" 2>/dev/null || true)
    if [ -d "$candidate" ] && [ -w "$candidate" ]; then
      win_user="$candidate"
    fi
  fi
fi

if [ -n "$win_user" ] && [ -d "$win_user" ]; then
  win_bin="$win_user/.local/bin"
  mkdir -p "$win_bin" 2>/dev/null || true
  if [ -w "$win_bin" ]; then
    python3 -c "
import os
win_bin = '$win_bin'
shim = '''@echo off
setlocal
where wsl.exe >nul 2>&1
if errorlevel 1 (
  echo wsl.exe not found - uachat runs inside WSL2 Ubuntu. 1>&2
  exit /b 1
)
set \"WSLENV=%WSLENV%:OPENAI_API_KEY/u:UNREAL_HARNESS_LLM_API_KEY/u:UNREAL_HARNESS_LLM_PROVIDER/u:UNREAL_HARNESS_LLM_MODEL/u:UNREAL_HARNESS_LLM_BASE_URL/u:UNREAL_HARNESS_LLM_MAX_ATTEMPTS/u:UACHAT_PROVIDER/u:UACHAT_THINKING/u:UACHAT_THEME/u:UACHAT_NOTIFY/u:UACHAT_AUTO_UPDATE/u:UACHAT_COLOR/u:UACHAT_RTK/u:UACHAT_CONTEXT_WINDOW/u:UACHAT_BRIDGE_TARGET/u:UACHAT_BRIDGE_KEY/u:UACHAT_ANTIGRAVITY_CLIENT_ID/u:UACHAT_ANTIGRAVITY_CLIENT_SECRET/u\"
wsl.exe -d Ubuntu -- uachat %*
exit /b %errorlevel%
'''
runner_shim = '''@echo off
setlocal
where wsl.exe >nul 2>&1
if errorlevel 1 (
  echo wsl.exe not found - unreal-agent-runner runs inside WSL2 Ubuntu. 1>&2
  exit /b 1
)
set \"WSLENV=%WSLENV%:OPENAI_API_KEY/u:UNREAL_HARNESS_LLM_API_KEY/u:UNREAL_HARNESS_LLM_PROVIDER/u:UNREAL_HARNESS_LLM_MODEL/u:UNREAL_HARNESS_LLM_BASE_URL/u:UNREAL_HARNESS_LLM_MAX_ATTEMPTS/u:UACHAT_PROVIDER/u:UACHAT_THINKING/u:UACHAT_THEME/u:UACHAT_NOTIFY/u:UACHAT_AUTO_UPDATE/u:UACHAT_COLOR/u:UACHAT_RTK/u:UACHAT_CONTEXT_WINDOW/u:UACHAT_BRIDGE_TARGET/u:UACHAT_BRIDGE_KEY/u:UACHAT_ANTIGRAVITY_CLIENT_ID/u:UACHAT_ANTIGRAVITY_CLIENT_SECRET/u\"
wsl.exe -d Ubuntu -- unreal-agent-runner %*
exit /b %errorlevel%
'''
uar_shim = '''@echo off
call \"%~dp0unreal-agent-runner.cmd\" %*
'''
for name, content in [('uachat.cmd', shim), ('unreal-chat.cmd', shim), ('unreal-agent-runner.cmd', runner_shim), ('uar.cmd', uar_shim)]:
    path = os.path.join(win_bin, name)
    try:
        with open(path, 'wb') as f:
            f.write(content.replace('\n', '\r\n').encode('ascii'))
    except OSError:
        pass
" 2>/dev/null || true
    echo "  Windows shims installed to $win_bin (uachat.cmd, uar.cmd)"
  else
    echo "  Windows bin dir not writable ($win_bin); skipping shims."
  fi
else
  echo "  Native Linux detected (not WSL or no Windows host profile); skipping Windows shims."
fi

echo "=== 6. Initializing configuration ==="
mkdir -p "$HOME/.config/uachat"
CONFIG_FILE="$HOME/.config/uachat/env"
if [ ! -f "$CONFIG_FILE" ]; then
  # Own configuration only. External credential migration is explicitly opt-in.
    cat > "$CONFIG_FILE" <<'EOF'
# uachat configuration
UNREAL_HARNESS_LLM_MODEL=deepseek-v4.1-flash
UNREAL_HARNESS_LLM_MAX_ATTEMPTS=3
UACHAT_THINKING=max
UACHAT_THEME=midnight
UACHAT_PROVIDER=opencode-go
# To use OpenCode Go bridge:
# UACHAT_BRIDGE_TARGET=https://opencode.ai/zen/go/v1
# UACHAT_BRIDGE_KEY=sk-...
EOF
    chmod 600 "$CONFIG_FILE"
    echo "  Created template config at $CONFIG_FILE"
else
  echo "  Existing config kept: $CONFIG_FILE"
fi

echo "=== 7. Installing git pre-commit hook ==="
if [ -d ".git" ]; then
  mkdir -p .git/hooks
  cat > .git/hooks/pre-commit <<'HOOK'
#!/usr/bin/env bash
exec "$(git rev-parse --show-toplevel)/check-secrets.sh"
HOOK
  chmod +x .git/hooks/pre-commit
  echo "  Pre-commit secret scanner hooked."
fi

echo "=== 8. Verification ==="
echo -n "  uachat version: "
uachat --version
echo -n "  core binary: "
python3 -c "import subprocess; res = subprocess.run(['unreal-agent-runner', '-h'], capture_output=True, text=True); print(res.stdout.splitlines()[0] if res.stdout else 'installed')"

echo
echo "Installation complete!"
echo "Run 'uachat' to start chatting, or 'uachat --help' for options."
