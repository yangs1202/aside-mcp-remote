"""Expose the local Aside MCP stdio server through Streamable HTTP."""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
MCP_REQUEST_TIMEOUT = float(os.environ.get("MCP_REQUEST_TIMEOUT_SECONDS", "180"))
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
MCP_CORS_ORIGIN = os.environ.get("MCP_CORS_ORIGIN", "")


def aside_command() -> str:
    configured = os.environ.get("ASIDE_BIN")
    if configured:
        return configured
    return shutil.which("aside") or "aside"


class AsideMcpSession:
    """Keep one local ``aside mcp`` stdio server per HTTP MCP session."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [aside_command(), "mcp"],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.messages: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self._read_messages, daemon=True)
        self.reader.start()

    def _read_messages(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                self.messages.put(message)
        self.messages.put(None)

    def request(self, message: Dict[str, object]) -> Dict[str, object]:
        if self.process.poll() is not None:
            raise RuntimeError("aside mcp process is not running")
        assert self.process.stdin is not None
        with self.lock:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            deadline = time.monotonic() + MCP_REQUEST_TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("aside mcp request timed out")
                response = self.messages.get(timeout=remaining)
                if response is None:
                    raise RuntimeError("aside mcp process closed stdout")
                if response.get("id") == message.get("id"):
                    return response

    def notify(self, message: Dict[str, object]) -> None:
        if self.process.poll() is not None:
            raise RuntimeError("aside mcp process is not running")
        assert self.process.stdin is not None
        with self.lock:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()


sessions: Dict[str, AsideMcpSession] = {}
sessions_lock = threading.Lock()


def close_session(session_id: str) -> None:
    with sessions_lock:
        session = sessions.pop(session_id, None)
    if session is not None:
        session.close()


def get_session(session_id: Optional[str]) -> Optional[AsideMcpSession]:
    if not session_id:
        return None
    with sessions_lock:
        return sessions.get(session_id)


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self) -> None:
        if MCP_CORS_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", MCP_CORS_ORIGIN)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Mcp-Session-Id, MCP-Protocol-Version")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")

    def _send_json(self, payload: Dict[str, object], status: int = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(encoded)

    def _authorized(self) -> bool:
        if not MCP_BEARER_TOKEN:
            return True
        return self.headers.get("Authorization") == f"Bearer {MCP_BEARER_TOKEN}"

    def do_OPTIONS(self) -> None:
        if urlsplit(self.path).path != "/mcp":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/status":
            with sessions_lock:
                session_count = len(sessions)
            self._send_json(
                {
                    "message": "Aside MCP Remote is running",
                    "mcp": "/mcp",
                    "mcp_sessions": session_count,
                }
            )
            return
        if path == "/mcp":
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "POST, DELETE")
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()
            return
        if path in {"/server.py", "/.env"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        super().do_GET()

    def do_DELETE(self) -> None:
        if urlsplit(self.path).path != "/mcp":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._authorized():
            self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        session_id = self.headers.get("Mcp-Session-Id")
        if not session_id:
            self.send_error(HTTPStatus.BAD_REQUEST, "Mcp-Session-Id is required")
            return
        close_session(session_id)
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/mcp":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._authorized():
            self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            message = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._send_json({"error": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self._send_json(
                {"error": "a single JSON-RPC 2.0 message is required"},
                HTTPStatus.BAD_REQUEST,
            )
            return

        method = message.get("method")
        session_id = self.headers.get("Mcp-Session-Id")
        session = get_session(session_id)
        if method == "initialize" and session is None:
            session_id = uuid.uuid4().hex
            session = AsideMcpSession()
            with sessions_lock:
                sessions[session_id] = session
            try:
                response = session.request(message)
            except Exception as error:
                close_session(session_id)
                self._send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "error": {"code": -32000, "message": str(error)},
                    },
                    HTTPStatus.BAD_GATEWAY,
                )
                return
            self._send_mcp_response(response, session_id)
            return
        if session is None:
            self._send_json(
                {"error": "valid Mcp-Session-Id is required"},
                HTTPStatus.BAD_REQUEST,
            )
            return

        if "id" not in message:
            try:
                session.notify(message)
            except Exception:
                close_session(session_id or "")
                self.send_error(HTTPStatus.BAD_GATEWAY)
                return
            self.send_response(HTTPStatus.ACCEPTED)
            self.send_header("Mcp-Session-Id", session_id or "")
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()
            return

        try:
            response = session.request(message)
        except TimeoutError as error:
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -32000, "message": str(error)},
                },
                HTTPStatus.GATEWAY_TIMEOUT,
            )
            return
        except Exception as error:
            close_session(session_id or "")
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -32000, "message": str(error)},
                },
                HTTPStatus.BAD_GATEWAY,
            )
            return
        self._send_mcp_response(response, session_id or "")

    def _send_mcp_response(self, response: Dict[str, object], session_id: str) -> None:
        encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Mcp-Session-Id", session_id)
        self.send_header("MCP-Protocol-Version", "2025-06-18")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        print(f"web {self.address_string()} - {format % args}", flush=True)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8766")))
    args = parser.parse_args()

    os.chdir(ROOT)
    server = Server((args.host, args.port), Handler)
    print(f"Aside MCP Remote listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        for session_id in list(sessions):
            close_session(session_id)


if __name__ == "__main__":
    main()
