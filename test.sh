#!/usr/bin/env bash
# Isolated regression suite; never modifies the user's config or sessions.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m unittest -v test_client test_bridge test_editor_auth test_antigravity test_runner test_auth_pool
./check-secrets.sh
