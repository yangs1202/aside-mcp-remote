"""Expose the local Aside MCP stdio server through Streamable HTTP."""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent
MCP_REQUEST_TIMEOUT = float(os.environ.get("MCP_REQUEST_TIMEOUT_SECONDS", "180"))
MCP_SESSION_TTL = float(os.environ.get("MCP_SESSION_TTL_SECONDS", "900"))
MCP_SESSION_PRESSURE_IDLE = float(os.environ.get("MCP_SESSION_PRESSURE_IDLE_SECONDS", "60"))
MCP_MAX_SESSIONS = int(os.environ.get("MCP_MAX_SESSIONS", "64"))
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
MCP_CORS_ORIGIN = os.environ.get("MCP_CORS_ORIGIN", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "aside-browser")
OPENAI_PROGRESS_MESSAGE = os.environ.get(
    "OPENAI_PROGRESS_MESSAGE", "요청을 처리하고 있어요."
)
TASK_START_TIMEOUT = float(os.environ.get("TASK_START_TIMEOUT_SECONDS", "30"))
MAX_TASK_HOSTS = 20
ASIDE_STATE_DIR = Path(
    os.environ.get("ASIDE_STATE_DIR", str(Path.home() / ".aside" / "u"))
)
HOST_TASK_STATE_DB = Path(
    os.environ.get(
        "HOST_TASK_STATE_DB",
        str(Path.home() / ".aside-mcp-remote" / "tasks.db"),
    )
).expanduser()
SESSION_ID_RE = re.compile(r"created new session:\s*(\S+)")
ACTIVE_SESSION_STATUSES = {"running", "queued", "streaming"}
FAILED_SESSION_STATUSES = {"errored", "aborted"}


def aside_command() -> str:
    configured = os.environ.get("ASIDE_BIN")
    if configured:
        return configured
    return shutil.which("aside") or "aside"


class AsideMcpSession:
    """Keep one ``aside mcp`` stdio server per HTTP MCP session."""

    def __init__(self, account: Optional[str] = None, host: Optional[str] = None) -> None:
        command = [aside_command(), "mcp"]
        if account:
            command.extend(["--account", account])
        if host is not None:
            command.extend(["--host", host])
        self.process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.messages: queue.Queue = queue.Queue()
        self.lock = threading.RLock()
        self.last_used = time.monotonic()
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
        with self.lock:
            try:
                return self._request(message)
            finally:
                self.last_used = time.monotonic()

    def _request(self, message: Dict[str, object]) -> Dict[str, object]:
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
        with self.lock:
            try:
                self._notify(message)
            finally:
                self.last_used = time.monotonic()

    def _notify(self, message: Dict[str, object]) -> None:
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
                self.process.wait()
        self.reader.join(timeout=3)
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe is not None:
                pipe.close()


def initialize_aside_session(
    client_name: str,
    account: Optional[str] = None,
    host: Optional[str] = None,
) -> AsideMcpSession:
    session = AsideMcpSession(account, host)
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


def read_catalog_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def available_models() -> list:
    aside_home = Path(os.environ.get("ASIDE_HOME", str(Path.home() / ".aside")))
    accounts = read_catalog_json(aside_home / "accounts.json")
    account_id = accounts.get("currentAccountId", 0)
    # Account IDs are directory indices, not arbitrary paths.
    if not isinstance(account_id, int) or account_id < 0:
        account_id = 0
    account_dir = aside_home / "u" / str(account_id)
    providers = read_catalog_json(account_dir / "models.json").get("providers", {})
    cached = read_catalog_json(account_dir / "cache" / "models-catalog.json")
    result = {OPENAI_MODEL: {"id": OPENAI_MODEL, "object": "model",
                            "created": 0, "owned_by": "aside"}}

    def add(provider: str, model: Any) -> None:
        if not isinstance(model, dict) or model.get("type", "chat") != "chat":
            return
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id:
            return
        full_id = f"{provider}/{model_id}"
        try:
            selected_model(full_id)
        except ValueError:
            return
        entry = {"id": full_id, "object": "model", "created": 0, "owned_by": provider}
        if isinstance(model.get("name"), str):
            entry["name"] = model["name"]
        result[full_id] = entry

    if isinstance(providers, dict):
        for provider, config in providers.items():
            if not isinstance(config, dict):
                continue
            configured = config.get("models", [])
            if isinstance(configured, list):
                for model in configured:
                    add(provider, model)
            # OAuth catalogs are scoped to this account. Intersect with the
            # provider catalog so internal/non-chat model IDs are not exposed.
            account_catalog = config.get("accountModelCatalog", {})
            allowed = account_catalog.get("modelIds", []) if isinstance(account_catalog, dict) else []
            provider_cache = cached.get(provider, {})
            models = provider_cache.get("models", []) if isinstance(provider_cache, dict) else []
            if isinstance(allowed, list) and isinstance(models, list):
                for model in models:
                    if isinstance(model, dict) and model.get("id") in allowed:
                        add(provider, model)
    return list(result.values())


def selected_model(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value):
        raise ValueError("model must be a model ID or provider/model ID")
    return None if value == OPENAI_MODEL else value


def call_aside_cli(prompt: str, model: str) -> str:
    try:
        result = subprocess.run(
            [aside_command(), "exec", "--model", model, "--", prompt],
            cwd=str(ROOT), stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=MCP_REQUEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("Aside model request timed out") from error
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout).strip()
    if result.returncode or re.search(r"(?m)^\s*•\s*Error\b", output):
        raise RuntimeError(output or "Aside model request failed")
    if not output:
        raise RuntimeError("Aside model request returned no text")
    return output


def call_aside_exec(
    prompt: str,
    model: Optional[str] = None,
    account: Optional[str] = None,
    host: Optional[str] = None,
) -> str:
    if model is not None:
        return call_aside_cli(prompt, model)
    session = initialize_aside_session("aside-mcp-remote-openai", account, host)
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


def account_index(account: Optional[str]) -> int:
    selected = account if account is not None else os.environ.get("ASIDE_ACCOUNT", "u0")
    if not re.fullmatch(r"u\d+", selected):
        raise ValueError("account must look like u0")
    return int(selected[1:])


def state_db_path(account: Optional[str] = None) -> Path:
    return ASIDE_STATE_DIR / str(account_index(account)) / "state.db"


def strip_ansi(value: str) -> str:
    return re.sub(r"\[[0-9;]*m", "", value)


class TaskStartError(RuntimeError):
    pass


def start_aside_exec(prompt: str, account: Optional[str] = None) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is required")
    command = [aside_command(), "exec", prompt]
    if account:
        if not re.fullmatch(r"u\d+", account):
            raise ValueError("account must look like u0")
        command[1:1] = ["--account", account]
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stderr is not None
    deadline = time.monotonic() + TASK_START_TIMEOUT
    captured = ""
    while time.monotonic() < deadline:
        line = process.stderr.readline()
        if line:
            captured += line
            match = SESSION_ID_RE.search(strip_ansi(line))
            if match:
                threading.Thread(
                    target=_drain_task_process,
                    args=(process,),
                    daemon=True,
                ).start()
                return match.group(1)
        elif process.poll() is not None:
            remainder = process.stderr.read() or ""
            captured += remainder
            match = SESSION_ID_RE.search(strip_ansi(captured))
            if match:
                return match.group(1)
            detail = strip_ansi(captured).strip() or f"aside exec exited {process.returncode}"
            raise TaskStartError(detail)
        else:
            time.sleep(0.02)
    process.terminate()
    raise TimeoutError("aside exec did not return a session id")


def start_host_task(prompt: str, account: Optional[str], host: str) -> str:
    task_id = f"host_{uuid.uuid4().hex}"
    state: Dict[str, Any] = {
        "taskId": task_id,
        "status": "running",
        "asideStatus": "running",
        "host": host,
        "updatedAt": int(time.time()),
    }
    save_host_task(state)
    with host_tasks_lock:
        host_tasks[task_id] = state
    threading.Thread(
        target=_run_host_task,
        args=(task_id, prompt, account, host),
        daemon=True,
    ).start()
    return task_id


host_tasks: Dict[str, Dict[str, Any]] = {}
host_tasks_lock = threading.Lock()


def save_host_task(state: Dict[str, Any]) -> None:
    HOST_TASK_STATE_DB.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(HOST_TASK_STATE_DB, timeout=1)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS host_tasks (task_id TEXT PRIMARY KEY, state TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO host_tasks (task_id, state) VALUES (?, ?)",
            (state["taskId"], json.dumps(state, ensure_ascii=False)),
        )
        connection.commit()
    finally:
        connection.close()
    HOST_TASK_STATE_DB.chmod(0o600)


def read_host_task(task_id: str) -> Optional[Dict[str, Any]]:
    with host_tasks_lock:
        state = host_tasks.get(task_id)
        if state is not None:
            return dict(state)
    if not HOST_TASK_STATE_DB.is_file():
        return None
    uri = HOST_TASK_STATE_DB.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1)
    try:
        row = connection.execute(
            "SELECT state FROM host_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        connection.close()
    return json.loads(row[0]) if row is not None else None


def initialize_host_task_store() -> None:
    HOST_TASK_STATE_DB.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(HOST_TASK_STATE_DB, timeout=1)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS host_tasks (task_id TEXT PRIMARY KEY, state TEXT NOT NULL)"
        )
        rows = connection.execute("SELECT task_id, state FROM host_tasks").fetchall()
        for task_id, encoded_state in rows:
            state = json.loads(encoded_state)
            if state.get("status") == "running":
                state["status"] = "interrupted"
                state["asideStatus"] = "interrupted"
                state["error"] = "Aside MCP 서비스가 재시작되어 Host 작업 실행 상태를 잃었습니다."
                state["updatedAt"] = int(time.time())
                connection.execute(
                    "UPDATE host_tasks SET state = ? WHERE task_id = ?",
                    (json.dumps(state, ensure_ascii=False), task_id),
                )
        connection.commit()
    finally:
        connection.close()
    HOST_TASK_STATE_DB.chmod(0o600)


def _run_host_task(task_id: str, prompt: str, account: Optional[str], host: str) -> None:
    try:
        result = call_aside_exec(prompt, account=account, host=host)
    except Exception as error:
        with host_tasks_lock:
            state = host_tasks.get(task_id)
            if state is not None:
                state["status"] = "failed"
                state["asideStatus"] = "errored"
                state["error"] = str(error)
                state["updatedAt"] = int(time.time())
                snapshot = dict(state)
            else:
                snapshot = None
        if snapshot is not None:
            save_host_task(snapshot)
        return
    with host_tasks_lock:
        state = host_tasks.get(task_id)
        if state is not None:
            state["status"] = "succeeded"
            state["asideStatus"] = "idle"
            state["raw"] = result
            state["result"] = result
            state["updatedAt"] = int(time.time())
            snapshot = dict(state)
        else:
            snapshot = None
    if snapshot is not None:
        save_host_task(snapshot)


def list_aside_hosts(account: Optional[str] = None) -> Dict[str, Any]:
    command = [aside_command(), "host", "list", "--json"]
    if account is not None:
        if not re.fullmatch(r"u\d+", account):
            raise ValueError("account must look like u0")
        command.extend(["--account", account])
    result = subprocess.run(
        command,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        detail = strip_ansi(result.stderr).strip()
        raise RuntimeError(detail or "aside host list failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("aside host list returned invalid JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("hosts"), list):
        raise RuntimeError("aside host list returned an invalid response")
    return payload


def _drain_task_process(process: subprocess.Popen) -> None:
    try:
        if process.stderr is not None:
            process.stderr.read()
        process.wait()
    except Exception:
        process.kill()


def read_task(task_id: str, account: Optional[str] = None) -> Dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", task_id):
        raise ValueError("invalid task id")
    state = read_host_task(task_id)
    if state is not None:
        return state
    database = state_db_path(account)
    if not database.is_file():
        raise FileNotFoundError("Aside session state is not available")
    uri = f"file:{database}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1)
    try:
        connection.row_factory = sqlite3.Row
        session = connection.execute(
            "SELECT id, status, updated_at FROM sessions WHERE id = ?",
            (task_id,),
        ).fetchone()
        if session is None:
            turn = None
        else:
            turn = connection.execute(
                """
                SELECT final_assistant_message, abort_reason, finished_at, aborted_at
                FROM session_turns
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
    finally:
        connection.close()
    if session is None:
        raise LookupError(task_id)
    return task_payload(session, turn)


def task_payload(session: sqlite3.Row, turn: Optional[sqlite3.Row]) -> Dict[str, Any]:
    aside_status = str(session["status"])
    finished = bool(turn and turn["finished_at"])
    aborted = bool(turn and turn["aborted_at"])
    if aside_status in ACTIVE_SESSION_STATUSES or (not finished and aside_status == "idle"):
        status = "running"
    elif aside_status in FAILED_SESSION_STATUSES or aborted:
        status = "failed"
    elif aside_status == "interrupted":
        status = "interrupted"
    else:
        status = "succeeded"
    payload: Dict[str, Any] = {
        "taskId": session["id"],
        "status": status,
        "asideStatus": aside_status,
        "updatedAt": session["updated_at"],
    }
    if status in {"succeeded", "failed", "interrupted"} and turn is not None:
        raw = turn["final_assistant_message"]
        if isinstance(raw, str) and raw:
            payload["raw"] = raw
            result = final_result_text(raw)
            if result:
                payload["result"] = result
        error = task_error(turn["abort_reason"])
        if error and status != "succeeded":
            payload["error"] = error
    return payload


def task_error(abort_reason: Any) -> Optional[str]:
    if not isinstance(abort_reason, str) or not abort_reason:
        return None
    try:
        parsed = json.loads(abort_reason)
    except json.JSONDecodeError:
        return abort_reason
    if isinstance(parsed, dict):
        error = parsed.get("error") or parsed.get("message")
        if isinstance(error, str) and error:
            return error
    return abort_reason


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


def final_result_text(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(parsed, list):
        parts = [message_text(item.get("content")) for item in parsed if isinstance(item, dict)]
        return "\n".join(part for part in parts if part)
    if isinstance(parsed, dict):
        return message_text(parsed.get("content", parsed.get("text", raw)))
    return raw


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
        session = sessions.get(session_id)
        if session is not None:
            session.last_used = time.monotonic()
        return session


def make_session_room() -> bool:
    """Called with sessions_lock held; retire the oldest idle session at capacity."""
    if len(sessions) < MCP_MAX_SESSIONS:
        return True
    for session_id, session in sorted(sessions.items(), key=lambda item: item[1].last_used):
        if not session.lock.acquire(blocking=False):
            continue
        try:
            if time.monotonic() - session.last_used < MCP_SESSION_PRESSURE_IDLE:
                continue
            sessions.pop(session_id)
            # Reclaim pipes before admitting another subprocess.
            session.close()
            print("MCP capacity: retired idle session", flush=True)
            return True
        finally:
            session.lock.release()
    return False


def reap_sessions() -> None:
    expired = []
    with sessions_lock:
        for session_id, session in list(sessions.items()):
            if not session.lock.acquire(blocking=False):
                continue
            try:
                if (session.process.poll() is not None or
                        time.monotonic() - session.last_used >= MCP_SESSION_TTL):
                    expired.append(sessions.pop(session_id))
            finally:
                session.lock.release()
    for session in expired:
        session.close()


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
        path = urlsplit(self.path).path
        if path not in {
            "/mcp",
            "/v1/models",
            "/v1/chat/completions",
            "/v1/hosts",
            "/v1/tasks",
        } and not path.startswith("/v1/tasks/"):
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
        if path.startswith("/v1/tasks/"):
            self._handle_get_task()
            return
        if path == "/v1/hosts":
            self._handle_get_hosts()
            return
        if path == "/v1/models":
            if not self._authorized():
                self._send_openai_error("unauthorized", HTTPStatus.UNAUTHORIZED, "authentication_error")
                return
            self._send_json({"object": "list", "data": available_models()})
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
        if get_session(session_id) is None:
            self._send_json({"error": "MCP session not found"}, HTTPStatus.NOT_FOUND)
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
        if path == "/v1/tasks":
            self._handle_create_task()
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
        if session_id and session is None:
            self._send_json({"error": "MCP session expired; initialize a new session"},
                            HTTPStatus.NOT_FOUND)
            return
        if method == "initialize" and session is None:
            session_id = uuid.uuid4().hex
            reap_sessions()
            try:
                with sessions_lock:
                    if not make_session_room():
                        self._send_json({"error": "MCP session capacity reached; retry later"},
                                        HTTPStatus.SERVICE_UNAVAILABLE)
                        return
                    session = AsideMcpSession()
                    sessions[session_id] = session
            except OSError:
                self._send_json({"error": "Unable to start MCP process; retry later"},
                                HTTPStatus.SERVICE_UNAVAILABLE)
                return
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
            close_session(session_id or "")
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

    def _handle_create_task(self) -> None:
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
        account = request.get("account")
        if account is not None and not isinstance(account, str):
            self._send_openai_error("account must be a string")
            return
        if account and not re.fullmatch(r"u\d+", account):
            self._send_openai_error("account must look like u0")
            return
        prompt = request.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            self._send_openai_error("prompt is required")
            return
        if "host" in request and "hosts" in request:
            self._send_openai_error("host and hosts cannot be used together")
            return
        host = request.get("host")
        if "host" in request and (not isinstance(host, str) or not host.strip()):
            self._send_openai_error("host must be a non-empty string")
            return
        hosts = request.get("hosts")
        if "hosts" in request:
            if not isinstance(hosts, list) or not hosts:
                self._send_openai_error("hosts must be a non-empty array")
                return
            if len(hosts) > MAX_TASK_HOSTS:
                self._send_openai_error(f"hosts may contain at most {MAX_TASK_HOSTS} entries")
                return
            if any(not isinstance(item, str) or not item.strip() for item in hosts):
                self._send_openai_error("each host must be a non-empty string")
                return
            hosts = [item.strip() for item in hosts]
            if len(set(hosts)) != len(hosts):
                self._send_openai_error("hosts must not contain duplicates")
                return
        elif host is not None:
            host = host.strip()

        if "hosts" in request:
            tasks = self._start_tasks_on_hosts(prompt, account, hosts)
            if any(task["status"] == "running" for task in tasks):
                status = HTTPStatus.ACCEPTED
            else:
                status = HTTPStatus.BAD_GATEWAY
            self._send_json({"tasks": tasks}, status)
            return
        try:
            if host is None:
                task_id = start_aside_exec(prompt, account)
            else:
                task_id = start_host_task(prompt, account, host)
        except ValueError as error:
            self._send_openai_error(str(error))
            return
        except TimeoutError as error:
            self._send_openai_error(str(error), HTTPStatus.GATEWAY_TIMEOUT, "timeout")
            return
        except Exception as error:
            print(f"task start failed: {error}", flush=True)
            self._send_openai_error(
                "Aside Browser 작업을 시작하지 못했습니다.",
                HTTPStatus.BAD_GATEWAY,
                "upstream_error",
            )
            return
        payload = {"taskId": task_id, "status": "running"}
        if host is not None:
            payload["host"] = host
        self._send_json(payload, HTTPStatus.ACCEPTED)

    def _start_tasks_on_hosts(
        self,
        prompt: str,
        account: Optional[str],
        hosts: list[str],
    ) -> list[Dict[str, str]]:
        tasks = []
        for host in hosts:
            try:
                task_id = start_host_task(prompt, account, host)
                tasks.append({"host": host, "taskId": task_id, "status": "running"})
            except Exception as error:
                print(f"task start failed for host {host}: {error}", flush=True)
                tasks.append(
                    {
                        "host": host,
                        "status": "failed",
                        "error": "Aside Host 작업을 시작하지 못했습니다.",
                    }
                )
        return tasks

    def _handle_get_hosts(self) -> None:
        if not self._authorized():
            self._send_openai_error("unauthorized", HTTPStatus.UNAUTHORIZED, "authentication_error")
            return
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        accounts = query.get("account", [])
        if len(accounts) > 1:
            self._send_openai_error("account may only be specified once")
            return
        account = accounts[0] if accounts else None
        try:
            payload = list_aside_hosts(account)
        except ValueError as error:
            self._send_openai_error(str(error))
            return
        except subprocess.TimeoutExpired:
            self._send_openai_error("Aside Host 목록 조회 시간이 초과됐습니다.", HTTPStatus.GATEWAY_TIMEOUT, "timeout")
            return
        except Exception as error:
            print(f"host list failed: {error}", flush=True)
            self._send_openai_error(
                "Aside Host 목록을 가져오지 못했습니다.",
                HTTPStatus.BAD_GATEWAY,
                "upstream_error",
            )
            return
        self._send_json(payload)

    def _handle_get_task(self) -> None:
        if not self._authorized():
            self._send_openai_error("unauthorized", HTTPStatus.UNAUTHORIZED, "authentication_error")
            return
        task_id = urlsplit(self.path).path.removeprefix("/v1/tasks/")
        if not task_id or "/" in task_id:
            self._send_openai_error("invalid task id")
            return
        account = None
        query = urlsplit(self.path).query
        if query:
            for part in query.split("&"):
                key, _, value = part.partition("=")
                if key == "account" and value:
                    account = value
        try:
            payload = read_task(task_id, account)
        except ValueError as error:
            self._send_openai_error(str(error))
            return
        except LookupError:
            self._send_openai_error("task not found", HTTPStatus.NOT_FOUND, "not_found")
            return
        except FileNotFoundError as error:
            self._send_openai_error(str(error), HTTPStatus.BAD_GATEWAY, "upstream_error")
            return
        except sqlite3.Error as error:
            print(f"task status failed: {error}", flush=True)
            self._send_openai_error(
                "Aside 작업 상태를 읽지 못했습니다.",
                HTTPStatus.BAD_GATEWAY,
                "upstream_error",
            )
            return
        self._send_json(payload)

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
        try:
            execution_model = selected_model(model)
        except ValueError as error:
            self._send_openai_error(str(error))
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
                content = call_aside_exec(prompt, execution_model)
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
            self._send_json(completion_payload(request_id, model, content, created))
            return

        self._send_sse_headers()
        try:
            self._send_sse(
                completion_chunk(
                    request_id,
                    model,
                    OPENAI_PROGRESS_MESSAGE,
                    created,
                )
            )
            content = call_aside_exec(prompt, execution_model)
            self._send_sse(completion_chunk(request_id, model, content, created))
            self._send_sse(
                completion_chunk(
                    request_id,
                    model,
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
                    model,
                    "Aside Browser 작업에 실패했습니다.",
                    created,
                )
            )
            self._send_sse(
                completion_chunk(
                    request_id,
                    model,
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

    def service_actions(self) -> None:
        reap_sessions()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8766")))
    args = parser.parse_args()

    def stop_service(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_service)
    os.chdir(ROOT)
    initialize_host_task_store()
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
