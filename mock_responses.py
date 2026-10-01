#!/usr/bin/env python3
"""Offline stand-in for the OpenAI Responses API (SSE).

Lets unreal-agent-runner complete real turns without credentials:
first request -> Bash function_call, any request containing a
function_call_output -> final assistant message.
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 11434


def sse(payload):
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"


def message_item(text, item_id="msg_1"):
    return {
        "type": "message",
        "id": item_id,
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def function_call_item(command, call_id="call_1"):
    return {
        "type": "function_call",
        "id": "fc_1",
        "call_id": call_id,
        "name": "Bash",
        "arguments": json.dumps({"command": command}),
        "status": "completed",
    }


def reasoning_item(text, item_id="rs_1"):
    return {
        "type": "reasoning",
        "id": item_id,
        "summary": [{"type": "summary_text", "text": text}],
    }


def response_obj(items, rid="resp_mock"):
    return {
        "id": rid,
        "status": "completed",
        "output": items,
        "usage": {
            "input_tokens": 1234,
            "output_tokens": 56,
            "input_tokens_details": {"cached_tokens": 1000},
            "output_tokens_details": {"reasoning_tokens": 12},
        },
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        text = raw.decode("utf-8", "replace")
        if "slow" in text:
            # Test hook: lets a client interrupt a turn that is still running.
            time.sleep(10)
        follow_up = "function_call_output" in text
        if follow_up:
            items = [message_item("Done. The command finished.")]
        else:
            items = [
                reasoning_item("Plan: run one command to prove the loop works."),
                function_call_item("echo mock-tool-output"),
            ]
        body = sse({"type": "response.completed", "response": response_obj(items)})
        body += b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("mock: " + fmt % args + "\n")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
