#!/usr/bin/env bash
# Unit tests for the issue-#11 workarounds: bridge.sanitize() (collapses
# duplicate tool outputs at request time) and repair-session.py (rewrites a
# stored session on disk). Both are pure functions, so no runner is needed.
set -uo pipefail
cd "$(dirname "$0")"
repo=$PWD

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
pass=0
fail=0

ok() {
  echo "ok   $1"
  pass=$((pass + 1))
}

bad() {
  echo "FAIL $1"
  fail=$((fail + 1))
}

echo "=== 1. bridge.sanitize collapses duplicates, keeps everything else ==="
if python3 - "$repo" "$tmp" <<'PY'
import importlib.util, json, os, sys

repo, tmp = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("bridge", os.path.join(repo, "bridge.py"))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

def body(items):
    return json.dumps({"input": items}).encode()

# duplicate call ids collapse to the last output, order and other items survive
payload = body([
    {"type": "message", "role": "user", "content": "hi"},
    {"type": "function_call_output", "call_id": "c1", "output": "started"},
    {"type": "function_call_output", "call_id": "c1", "output": "finished"},
    {"type": "function_call_output", "call_id": "c2", "output": "only"},
    {"type": "custom_tool_call_output", "call_id": "c2", "output": "custom"},
])
out, dropped = bridge.sanitize(payload)
items = json.loads(out)["input"]
assert dropped == 1, dropped
assert [i.get("output") for i in items if i["type"] == "function_call_output"] == ["finished", "only"], items
assert items[0]["type"] == "message" and len(items) == 4, items

# outputs without a call id are never collapsed (they all used to share one key)
out, dropped = bridge.sanitize(body([
    {"type": "function_call_output", "output": "a"},
    {"type": "function_call_output", "output": "b"},
]))
assert dropped == 0 and len(json.loads(out)["input"]) == 2

# nothing to collapse, malformed bodies and non-list input pass through verbatim
out, dropped = bridge.sanitize(b"not json")
assert out == b"not json" and dropped == 0
out, dropped = bridge.sanitize(json.dumps({"input": "nope"}).encode())
assert dropped == 0 and json.loads(out)["input"] == "nope"
print("bridge.sanitize: all assertions passed")
PY
then ok "bridge.sanitize assertions"; else bad "bridge.sanitize assertions"; fi

echo
echo "=== 2. repair-session.py collapses stored duplicates ==="
if python3 - "$repo" "$tmp" <<'PY'
import json, os, subprocess, sys

repo, tmp = sys.argv[1], sys.argv[2]
broken = os.path.join(tmp, "broken.session.jsonl")
fixed = os.path.join(tmp, "fixed.session.jsonl")
dry = os.path.join(tmp, "dry.session.jsonl")

sequence = 0
def item(kind, data):
    global sequence
    sequence += 1
    return {"type": "item", "data": {"Item": {
        "Sequence": sequence, "RecordedAt": "2026-01-01T00:00:00Z", "Kind": kind, "Data": data}}}

records = [{"type": "session", "data": {"Version": 2, "Session": {"ID": "broken"}}}]
records.append(item("input", {"Kind": "external", "Payload": "hello"}))
records.append(item("tool_call_status", {"CallID": "c1", "Status": {"Error": ""}}))
records.append(item("tool_call_status", {"CallID": "c1", "Status": {"Error": ""}}))       # identical -> dropped
records.append(item("tool_call_status", {"CallID": "c2", "Status": {"Error": "boom"}}))
records.append(item("tool_call_status", {"CallID": "c2", "Status": {"Error": ""}}))        # different -> dropped too
records.append({"type": "operation", "data": {"Operation": {"ID": "op1", "Type": "shell"}}})
with open(broken, "w", encoding="utf-8") as handle:
    for record in records:
        handle.write(json.dumps(record) + "\n")
before = open(broken, "rb").read()

script = os.path.join(repo, "repair-session.py")
proc = subprocess.run([sys.executable, script, broken, "--dry-run", "--output", dry], capture_output=True, text=True)
assert proc.returncode == 0, proc.stderr
assert "dropped: 2" in proc.stdout and "different_duplicates: 1" in proc.stdout, proc.stdout
assert not os.path.exists(dry), "dry run must not write"

proc = subprocess.run([sys.executable, script, broken, "--output", fixed], capture_output=True, text=True)
assert proc.returncode == 0, proc.stderr
assert "dropped: 2" in proc.stdout, proc.stdout
assert "resume with: uachat -s fixed" in proc.stdout, proc.stdout
assert open(broken, "rb").read() == before, "the original file must stay untouched"

lines = open(fixed, encoding="utf-8").read().splitlines()
items = [json.loads(line)["data"]["Item"] for line in lines if '"item"' in line]
statuses = [i for i in items if i["Kind"] == "tool_call_status"]
assert [s["Data"]["CallID"] for s in statuses] == ["c1", "c2"], statuses
assert statuses[-1]["Data"]["Status"]["Error"] == "", statuses          # the final status wins
sequences = [i["Sequence"] for i in items]
assert sequences == list(range(1, len(sequences) + 1)), sequences
assert (os.stat(fixed).st_mode & 0o777) == 0o600, oct(os.stat(fixed).st_mode)

# --keep-different preserves the earlier non-identical record
kept = os.path.join(tmp, "kept.session.jsonl")
proc = subprocess.run([sys.executable, script, broken, "--output", kept, "--keep-different"], capture_output=True, text=True)
assert proc.returncode == 0 and "dropped: 1" in proc.stdout and "different_duplicates_kept: 1" in proc.stdout, proc.stdout
lines = open(kept, encoding="utf-8").read().splitlines()
items = [json.loads(line)["data"]["Item"] for line in lines if '"item"' in line]
assert len([i for i in items if i["Kind"] == "tool_call_status"]) == 3, items

# repairing the repaired copy is a no-op
proc = subprocess.run([sys.executable, script, fixed, "--output", "again"], capture_output=True, text=True)
assert proc.returncode == 0 and "dropped: 0" in proc.stdout, proc.stdout
print("repair-session.py: all assertions passed")
PY
then ok "repair-session.py assertions"; else bad "repair-session.py assertions"; fi

echo
echo "=== 3. the client wraps it as uachat --repair ==="
out=$(python3 uarchat.py --repair "$tmp/broken.session.jsonl" 2>&1)
if printf '%s' "$out" | grep -qF 'resume with: uachat -s broken-rep'; then
  ok "prints the resume command"
else
  bad "prints the resume command (got: $out)"
fi
if [ -f "$tmp/broken-rep.session.jsonl" ]; then ok "wrote the repaired copy"; else bad "wrote the repaired copy"; fi

echo
echo "=== 4. duplicate tool output failures print the repair hint ==="
if python3 - <<'PY'
import contextlib, importlib.util, io, os

spec = importlib.util.spec_from_file_location("uarchat", os.path.abspath("uarchat.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

theme = module.Theme("midnight", False)
buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    module.repair_hint(theme, "my-chat", ["runner: invalid_request_error: Duplicate tool output for call_id: call_1"])
text = buffer.getvalue()
assert "uachat --repair my-chat" in text, text
buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    module.repair_hint(theme, "my-chat", ["some unrelated failure"])
assert buffer.getvalue() == "", buffer.getvalue()
print("repair_hint: all assertions passed")
PY
then ok "repair_hint assertions"; else bad "repair_hint assertions"; fi

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
