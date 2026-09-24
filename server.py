"""Expose the local Aside MCP stdio server through Streamable HTTP."""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
MCP_REQUEST_TIMEOUT = float(os.environ.get("MCP_REQUEST_TIMEOUT_SECONDS", "180"))
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
MCP_CORS_ORIGIN = os.environ.get("MCP_CORS_ORIGIN", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "aside-browser")
OPENAI_PROGRESS_MESSAGE = os.environ.get(
    "OPENAI_PROGRESS_MESSAGE", "요청을 처리하고 있어요."
)


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


def initialize_aside_session(client_name: str) -> AsideMcpSession:
    session = AsideMcpSession()
    try:
        response = session.request(
            {
                "jsonrpc": "2.0",
                "id": uuid.uuid4().hex,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": client_name, "version": "1.0"},
                },
            }
        )
        if "error" in response:
            raise RuntimeError(str(response["error"]))
        session.notify(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
        )
        return session
    except Exception:
        session.close()
        raise


def call_aside_exec(prompt: str) -> str:
    session = initialize_aside_session("aside-mcp-remote-openai")
    try:
        response = session.request(
            {
                "jsonrpc": "2.0",
                "id": uuid.uuid4().hex,
                "method": "tools/call",
                "params": {
                    "name": "exec",
                    "arguments": {"prompt": prompt},
                },
            }
        )
        if "error" in response:
            raise RuntimeError(str(response["error"]))
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise RuntimeError("aside exec returned an invalid result")
        if result.get("isError"):
            raise RuntimeError(extract_tool_text(result) or "aside exec failed")
        text = extract_tool_text(result)
        if not text:
            raise RuntimeError("aside exec returned no text")
        return text
    finally:
        session.close()


def extract_tool_text(result: Dict[str, Any]) -> str:
    parts = []
    for item in result.get("content", []) or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return re.sub(r"^session_id:\s*[^\n]+\n*", "", "\n".join(parts)).strip()


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content) if content is not None else ""


def messages_to_prompt(messages: Any) -> str:
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty array")
    lines = [
        "You are answering through an OpenAI-compatible bridge backed by Aside Browser.",
        "Use the browser when the user asks for current or web-based information.",
        "Return only the final answer to the user; do not mention this bridge or internal session IDs.",
        "",
        "Conversation:",
    ]
    has_text = False
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("each message must be an object")
        role = str(message.get("role", "user"))
        content = message_text(message.get("content"))
        if content:
            has_text = True
            lines.append(f"{role}: {content}")
    if not has_text:
        raise ValueError("messages must contain text")
    return "\n".join(lines)


def completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def completion_payload(
    request_id: str,
    model: str,
    content: str,
    created: int,
) -> Dict[str, Any]:
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def completion_chunk(
    request_id: str,
    model: str,
    content: str,
    created: int,
    finish_reason: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content},
                "finish_reason": finish_reason,
            }
        ],
    }


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
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization, Mcp-Session-Id, MCP-Protocol-Version",
            )
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

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length))

    def _send_openai_error(
        self,
        message: str,
        status: int = HTTPStatus.BAD_REQUEST,
        error_type: str = "invalid_request_error",
    ) -> None:
        self._send_json(
            {"error": {"message": message, "type": error_type}},
            status,
        )

    def _send_sse(self, payload: Dict[str, Any]) -> None:
        encoded = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode(
            "utf-8"
        )
        self.wfile.write(encoded)
        self.wfile.flush()

    def _send_sse_headers(self) -> None:
        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

    def do_OPTIONS(self) -> None:
        if urlsplit(self.path).path not in {
            "/mcp",
            "/v1/models",
            "/v1/chat/completions",
        }:
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
        if path == "/v1/models":
            if not self._authorized():
                self._send_openai_error("unauthorized", HTTPStatus.UNAUTHORIZED, "authentication_error")
                return
            now = int(time.time())
            self._send_json(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": OPENAI_MODEL,
                            "object": "model",
                            "created": now,
                            "owned_by": "aside",
                        }
                    ],
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
        path = urlsplit(self.path).path
        if path == "/v1/chat/completions":
            self._handle_chat_completions()
            return
        if path != "/mcp":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._authorized():
            self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            message = self._read_json()
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

    def _handle_chat_completions(self) -> None:
        if not self._authorized():
            self._send_openai_error("unauthorized", HTTPStatus.UNAUTHORIZED, "authentication_error")
            return
        try:
            request = self._read_json()
        except (ValueError, json.JSONDecodeError):
            self._send_openai_error("invalid JSON")
            return
        if not isinstance(request, dict):
            self._send_openai_error("request body must be an object")
            return

        model = request.get("model", OPENAI_MODEL)
        if model != OPENAI_MODEL:
            self._send_openai_error(
                f"model '{model}' is not available",
                HTTPStatus.NOT_FOUND,
                "model_not_found",
            )
            return
        if request.get("tools"):
            self._send_openai_error(
                "tools are not supported by the Aside Browser adapter",
                HTTPStatus.BAD_REQUEST,
            )
            return
        stream = request.get("stream", False)
        if not isinstance(stream, bool):
            self._send_openai_error("stream must be a boolean")
            return
        try:
            prompt = messages_to_prompt(request.get("messages"))
        except ValueError as error:
            self._send_openai_error(str(error))
            return

        request_id = completion_id()
        created = int(time.time())
        if not stream:
            try:
                content = call_aside_exec(prompt)
            except TimeoutError as error:
                self._send_openai_error(str(error), HTTPStatus.GATEWAY_TIMEOUT, "timeout")
                return
            except Exception as error:
                print(f"openai adapter failed: {error}", flush=True)
                self._send_openai_error(
                    "Aside Browser 작업에 실패했습니다.",
                    HTTPStatus.BAD_GATEWAY,
                    "upstream_error",
                )
                return
            self._send_json(completion_payload(request_id, OPENAI_MODEL, content, created))
            return

        self._send_sse_headers()
        try:
            self._send_sse(
                completion_chunk(
                    request_id,
                    OPENAI_MODEL,
                    OPENAI_PROGRESS_MESSAGE,
                    created,
                )
            )
            content = call_aside_exec(prompt)
            self._send_sse(completion_chunk(request_id, OPENAI_MODEL, content, created))
            self._send_sse(
                completion_chunk(
                    request_id,
                    OPENAI_MODEL,
                    "",
                    created,
                    finish_reason="stop",
                )
            )
        except Exception as error:
            print(f"openai streaming adapter failed: {error}", flush=True)
            self._send_sse(
                completion_chunk(
                    request_id,
                    OPENAI_MODEL,
                    "Aside Browser 작업에 실패했습니다.",
                    created,
                )
            )
            self._send_sse(
                completion_chunk(
                    request_id,
                    OPENAI_MODEL,
                    "",
                    created,
                    finish_reason="stop",
                )
            )
        finally:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

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
