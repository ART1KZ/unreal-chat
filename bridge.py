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
import os
import sys
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Responses API bridge to OpenCode Go")
    parser.add_argument("--listen", default="127.0.0.1:8791", help="host:port to listen on (port 0 picks a free port)")
    parser.add_argument("--target", required=True, help="upstream base URL, e.g. https://opencode.ai/zen/go/v1")
    parser.add_argument("--key", default=os.environ.get("UACHAT_BRIDGE_KEY", ""), help="upstream API key (defaults to $UACHAT_BRIDGE_KEY; prefer the environment so it stays out of `ps`)")
    parser.add_argument("--session", required=True, help="value for x-opencode-session (stable per conversation)")
    parser.add_argument("--user-agent", default="uarchat/0.1", help="User-Agent sent upstream")
    args = parser.parse_args()
    if not args.key:
        parser.error("--key or UACHAT_BRIDGE_KEY is required")

    host, _, port_text = args.listen.rpartition(":")
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port_text)), make_handler(Bridge(args.target, args.key, args.session, args.user_agent)))
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
