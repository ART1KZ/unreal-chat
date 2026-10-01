#!/usr/bin/env python3
"""Local Responses API bridge to the OpenCode Go gateway.

The unreal-agent harness speaks the OpenAI Responses API but cannot set custom
headers, while OpenCode Go requires `x-opencode-session` (stable per
conversation) and a client-specific User-Agent. This bridge listens on
loopback, rewrites those headers, forwards the body verbatim, and streams the
upstream SSE response back.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHUNK = 8192
TIMEOUT_SECONDS = 600


class Bridge:
    def __init__(self, target: str, key: str, session: str, user_agent: str) -> None:
        self.target = target.rstrip("/")
        self.key = key
        self.session = session
        self.user_agent = user_agent

    def headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {self.key}",
            "x-opencode-session": self.session,
            "User-Agent": self.user_agent,
        }
        return headers


def make_handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            dump_request(body)
            body, dropped = sanitize(body)
            if dropped:
                message = f"collapsed {dropped} duplicate tool output(s) for {self.path}"
                sys.stderr.write(f"bridge: {message}\n")
                try:
                    log_path = os.path.join(os.path.expanduser("~/.local/state/uachat"), "bridge.log")
                    os.makedirs(os.path.dirname(log_path), exist_ok=True)
                    with open(log_path, "a", encoding="utf-8") as handle:
                        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
                except OSError:
                    pass
            path = self.path
            url = bridge.target + "/responses"
            request = urllib.request.Request(url, data=body, method="POST", headers=bridge.headers())
            status = 502
            try:
                upstream = urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)
                status = upstream.status
            except urllib.error.HTTPError as error:
                upstream = error
                status = error.code
            except OSError as error:
                sys.stderr.write(f"bridge: upstream error: {error}\n")
                payload = b'{"error":{"message":"bridge upstream unreachable"}}'
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            content_type = upstream.headers.get("Content-Type", "application/json")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Connection", "close")
            self.end_headers()
            total = 0
            try:
                while True:
                    chunk = upstream.read(CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                upstream.close()
            sys.stderr.write(f"bridge: {path} -> {status} ({total} bytes)\n")

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("bridge: " + fmt % args + "\n")

    return Handler


def sanitize(body: bytes) -> tuple[bytes, int]:
    """Collapse duplicate tool outputs for one call id.

    The harness writes one function_call_output per recorded tool-call status
    (in-progress + final), which some providers reject with
    "Duplicate tool output for call_id" (unreal-agent issue #11). Keep the last
    output per call id, which is the completed one.
    """
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return body, 0
    if not isinstance(payload, dict):
        return body, 0
    items = payload.get("input")
    if not isinstance(items, list):
        return body, 0
    index_by_key: dict[tuple, int] = {}
    kept: list = []
    dropped = 0
    for item in items:
        if isinstance(item, dict) and item.get("type") in ("function_call_output", "custom_tool_call_output"):
            call_id = item.get("call_id") or item.get("item_id") or ""
            key = (item.get("type"), call_id)
            # Only outputs that name a call id can be duplicates; never collapse
            # anonymous ones (they would all share the same key).
            if call_id and key in index_by_key:
                kept[index_by_key[key]] = item
                dropped += 1
                continue
            if call_id:
                index_by_key[key] = len(kept)
        kept.append(item)
    if not dropped:
        return body, 0
    payload["input"] = kept
    return json.dumps(payload, ensure_ascii=False).encode(), dropped


def dump_request(body: bytes) -> None:
    if (os.environ.get("UACHAT_BRIDGE_DEBUG") or "").strip() in ("", "0", "off", "false", "no"):
        return
    try:
        path = os.path.join(os.path.expanduser("~/.local/state/uachat"), "bridge-last-request.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(body)
        os.chmod(path, 0o600)
    except OSError:
        pass


def _watch_parent(parent_pid: int) -> None:
    """Exit once the client that spawned us is gone (systemd may adopt orphans)."""
    if parent_pid <= 1:
        return
    while True:
        time.sleep(2)
        if not os.path.exists(f"/proc/{parent_pid}"):
            os._exit(0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Responses API bridge to OpenCode Go")
    parser.add_argument("--listen", default="127.0.0.1:8791", help="host:port to listen on (port 0 picks a free port)")
    parser.add_argument("--target", required=True, help="upstream base URL, e.g. https://opencode.ai/zen/go/v1")
    parser.add_argument("--key", default=os.environ.get("UACHAT_BRIDGE_KEY", ""), help="upstream API key (defaults to $UACHAT_BRIDGE_KEY; prefer the environment so it stays out of `ps`)")
    parser.add_argument("--session", required=True, help="value for x-opencode-session (stable per conversation)")
    parser.add_argument("--user-agent", default="uarchat/0.1", help="User-Agent sent upstream")
    parser.add_argument("--parent-pid", type=int, default=int(os.environ.get("UACHAT_PARENT_PID", "0")), help="exit when this pid disappears (defaults to $UACHAT_PARENT_PID)")
    args = parser.parse_args()
    if not args.key:
        parser.error("--key or UACHAT_BRIDGE_KEY is required")

    host, _, port_text = args.listen.rpartition(":")
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port_text)), make_handler(Bridge(args.target, args.key, args.session, args.user_agent)))
    threading.Thread(target=_watch_parent, args=(args.parent_pid,), daemon=True).start()
    print(f"listening on {server.server_address[0]}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
