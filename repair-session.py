#!/usr/bin/env python3
"""Repair a stored unreal-agent session that the gateway rejects.

The harness records a tool-call status twice for the same CallID (identical
records), and builds one `function_call_output` per record, so providers that
validate the Responses history answer with
`invalid_request_error: Duplicate tool output for call_id: ...`
(unreal-agent issue #11). This tool writes a copy of the session with the
duplicate status records collapsed - the original file is never modified.

    repair-session.py <session-id-or-file> [--output <session-id>] [--dry-run]

Then resume the repaired session:  uachat -s <output-id>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

SESSION_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "unreal-agent",
    "sessions",
)


def session_file(name: str) -> str:
    if os.path.sep in name or name.endswith(".jsonl"):
        return name
    return os.path.join(SESSION_DIR, f"{name}.session.jsonl")


def unwrap(line: str) -> dict | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if record.get("type") == "item":
        return (record.get("data") or {}).get("Item") or {}
    return record


def repair(path: str, output: str, dry_run: bool = False) -> dict:
    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()

    statuses: dict[str, list[int]] = defaultdict(list)
    for index, line in enumerate(lines):
        item = unwrap(line)
        if item is None or item.get("Kind") != "tool_call_status":
            continue
        call_id = (item.get("Data") or {}).get("CallID") or ""
        if call_id:
            statuses[call_id].append(index)

    drop: set[int] = set()
    kept_duplicates = 0
    for call_id, indices in statuses.items():
        if len(indices) < 2:
            continue
        last = indices[-1]
        # Compare the payload only: Sequence/RecordedAt always differ.
        last_payload = json.dumps((unwrap(lines[last]) or {}).get("Data"), sort_keys=True)
        for index in indices[:-1]:
            payload = json.dumps((unwrap(lines[index]) or {}).get("Data"), sort_keys=True)
            if payload == last_payload:
                drop.add(index)
            else:
                kept_duplicates += 1

    kept_lines = [line for index, line in enumerate(lines) if index not in drop]
    renumbered = 0
    if drop:
        # The store validates sequence continuity (1..N), so renumber the survivors.
        sequence = 0
        result: list[str] = []
        for line in kept_lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                result.append(line)
                continue
            item = record.get("data", {}).get("Item") if record.get("type") == "item" else None
            if isinstance(item, dict) and "Sequence" in item:
                sequence += 1
                if item.get("Sequence") != sequence:
                    item["Sequence"] = sequence
                    renumbered += 1
                    line = json.dumps(record, ensure_ascii=False) + "\n"
            result.append(line)
        kept_lines = result

    summary = {
        "input": path,
        "output": output,
        "records": len(lines),
        "dropped": len(drop),
        "renumbered": renumbered,
        "calls": len(statuses),
        "calls_with_duplicates": sum(1 for indices in statuses.values() if len(indices) > 1),
        "different_duplicates_kept": kept_duplicates,
    }
    if dry_run:
        return summary

    with open(output, "w", encoding="utf-8") as handle:
        handle.writelines(kept_lines)
    os.chmod(output, 0o600)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Collapse duplicate tool-call status records in a session copy")
    parser.add_argument("session", help="session id or path to a .session.jsonl file")
    parser.add_argument("--output", help="output session id (default: <session>-rep)")
    parser.add_argument("--dry-run", action="store_true", help="report what would change")
    args = parser.parse_args()

    path = session_file(args.session)
    if not os.path.exists(path):
        raise SystemExit(f"session not found: {path}")
    stem = os.path.basename(path)[: -len(".session.jsonl")] if path.endswith(".session.jsonl") else os.path.basename(path)
    output_id = args.output or f"{stem}-rep"
    output = os.path.join(os.path.dirname(path), f"{output_id}.session.jsonl")
    if os.path.abspath(output) == os.path.abspath(path):
        raise SystemExit("output must differ from the input")

    summary = repair(path, output, args.dry_run)
    for key, value in summary.items():
        print(f"{key}: {value}")
    if not args.dry_run:
        print(f"resume with: uachat -s {output_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
