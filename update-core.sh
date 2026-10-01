#!/usr/bin/env bash
# Keep the unreal-agent runner in sync with upstream GitHub releases.
#
#   update-core.sh            install the latest release when the local stamp differs
#   update-core.sh --check    report status only
#   update-core.sh --force    reinstall even when the stamp matches
#   update-core.sh --quiet    speak only when something changed or failed
#
# The installed tag is recorded in ~/.local/state/uachat/core.version, and a
# one-line note is queued in ~/.local/state/uachat/pending-note for the next
# uachat start. Downloads are verified against the release SHA256SUMS.
set -euo pipefail

REPO="unreallabsai/unreal-agent"
BINARY="/usr/local/bin/unreal-agent-runner"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/uachat"
STAMP="$STATE_DIR/core.version"
NOTE="$STATE_DIR/pending-note"

MODE=install
FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE=check ;;
    --quiet) MODE=quiet ;;
    --force) FORCE=1 ;;
    *) echo "usage: update-core.sh [--check|--quiet] [--force]" >&2; exit 2 ;;
  esac
  shift
done

say() { [ "$MODE" = quiet ] || echo "$@"; }
queue_note() { mkdir -p "$STATE_DIR"; printf '%s\n' "$1" > "$NOTE"; }

case "$(uname -m)" in
  x86_64 | amd64) ARCH=amd64 ;;
  aarch64 | arm64) ARCH=arm64 ;;
  *) echo "update-core: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

latest=$(curl -fsSL --max-time 20 "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tag_name", ""))' 2>/dev/null || true)
if [ -z "$latest" ]; then
  say "core: cannot reach the GitHub releases API (offline or rate-limited)"
  exit 0
fi

installed=$(cat "$STAMP" 2>/dev/null || true)

if [ "$MODE" = check ]; then
  if [ -f "$NOTE" ]; then cat "$NOTE"; fi
  if [ "$latest" = "$installed" ] && [ -x "$BINARY" ]; then
    echo "core: $installed is up to date"
  else
    echo "core: ${installed:-none} installed, $latest available"
  fi
  exit 0
fi

if [ "$latest" = "$installed" ] && [ -x "$BINARY" ] && [ "$FORCE" = 0 ]; then
  say "core: $installed is up to date"
  exit 0
fi

version=${latest#v}
asset="unreal-agent-runner_${version}_linux_${ARCH}.tar.gz"
base="https://github.com/$REPO/releases/download/$latest"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL --max-time 300 -o "$work/$asset" "$base/$asset"
curl -fsSL --max-time 60 -o "$work/SHA256SUMS" "$base/SHA256SUMS"
(cd "$work" && sha256sum -c --ignore-missing --status SHA256SUMS)
tar -xzf "$work/$asset" -C "$work" unreal-agent-runner
sudo install -m 0755 "$work/unreal-agent-runner" "$BINARY"
mkdir -p "$STATE_DIR"
printf '%s\n' "$latest" > "$STAMP"
queue_note "core: updated ${installed:-none} → $latest"
say "core: installed $latest"
