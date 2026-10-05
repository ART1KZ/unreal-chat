#!/usr/bin/env bash
# Non-TTY CLI/config/session/status checks with temporary HOME and fake runner.
set -euo pipefail
export UACHAT_WINDOWS_HOME=off
cd "$(dirname "$0")"
python3 -m unittest -v test_client.ClientTests test_client.StatusTests
