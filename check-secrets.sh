#!/usr/bin/env bash
# Refuse to commit anything that looks like a credential.
# Scans tracked files and the working tree (minus ignored paths); skips itself,
# since the pattern list below obviously contains key-shaped prefixes.
set -euo pipefail
cd "$(dirname "$0")"

# Real keys are long: sk-… (OpenAI/OpenCode/Fireworks), gho_/ghp_/github_pat_ (GitHub).
# Short placeholders like `sk-...` in docs must not trip the scan.
PATTERN='sk-[A-Za-z0-9_-]{24,}|gho_[A-Za-z0-9]{30,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}'

hits=$(git grep -nIE "$PATTERN" -- . ':!check-secrets.sh' 2>/dev/null || true)
if [ -z "$hits" ]; then
  hits=$(grep -rInIE "$PATTERN" . --exclude-dir=.git --exclude-dir=__pycache__ --exclude=check-secrets.sh 2>/dev/null || true)
fi

if [ -n "$hits" ]; then
  echo "secret-like content found:" >&2
  echo "$hits" >&2
  exit 1
fi

count=$(git ls-files | wc -l)
echo "secrets: clean ($count tracked files)"
