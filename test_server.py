import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import server
import sqlite3
from pathlib import Path


class OpenAIAdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def request(self, path, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        return urllib.request.urlopen(request, timeout=5)

    def test_models_lists_aside_browser(self):
        with self.request("/v1/models") as response:
            body = json.loads(response.read())
        self.assertEqual(body["data"][0]["id"], "aside-browser")

    def test_non_stream_chat_completion(self):
        with mock.patch.object(server, "call_aside_exec", return_value="노원구는 맑습니다."):
            with self.request(
                "/v1/chat/completions",
                {
                    "model": "aside-browser",
                    "messages": [{"role": "user", "content": "오늘 날씨?"}],
                },
            ) as response:
                body = json.loads(response.read())
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["choices"][0]["message"]["content"], "노원구는 맑습니다.")

    def test_stream_chat_completion_has_progress_and_done(self):
        with mock.patch.object(server, "call_aside_exec", return_value="결과입니다."):
            with self.request(
                "/v1/chat/completions",
                {
                    "model": "aside-browser",
                    "messages": [{"role": "user", "content": "확인해줘"}],
                    "stream": True,
                },
            ) as response:
                body = response.read().decode("utf-8")
        self.assertIn("요청을 처리하고 있어요.", body)
        self.assertIn("결과입니다.", body)
        self.assertTrue(body.endswith("data: [DONE]\n\n"))

    def test_invalid_model_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.request(
                "/v1/chat/completions",
                {
                    "model": "--invalid",
                    "messages": [{"role": "user", "content": "안녕"}],
                },
            )
        self.assertEqual(context.exception.code, 400)


class TasksApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def request(self, path, payload=None, method=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method or ("POST" if data is not None else "GET"),
        )
        return urllib.request.urlopen(request, timeout=5)

    def test_create_returns_session_id_without_waiting(self):
        with mock.patch.object(server, "start_aside_exec", return_value="ses_created") as start:
            with self.request("/v1/tasks", {"prompt": "서울 날씨 확인"}) as response:
                body = json.loads(response.read())
                status = response.status
        self.assertEqual(status, 202)
        self.assertEqual(body, {"taskId": "ses_created", "status": "running"})
        start.assert_called_once_with("서울 날씨 확인", None)

    def test_create_routes_task_to_one_host(self):
        with mock.patch.object(server, "start_host_task", return_value="host_remote01") as start:
            with self.request(
                "/v1/tasks",
                {"prompt": "호스트 확인", "account": "u1", "host": "gs-aside-worker01"},
            ) as response:
                body = json.loads(response.read())
        self.assertEqual(
            body,
            {"taskId": "host_remote01", "status": "running", "host": "gs-aside-worker01"},
        )
        start.assert_called_once_with("호스트 확인", "u1", "gs-aside-worker01")

    def test_create_starts_tasks_on_multiple_hosts(self):
        def start(prompt, account, host):
            return f"host_{host.replace('-', '_')}"

        with mock.patch.object(server, "start_host_task", side_effect=start) as start_mock:
            with self.request(
                "/v1/tasks",
                {"prompt": "작업 확인", "hosts": ["worker-one", "worker-two"]},
            ) as response:
                body = json.loads(response.read())
                status = response.status
        self.assertEqual(status, 202)
        self.assertEqual(
            body["tasks"],
            [
                {"host": "worker-one", "taskId": "host_worker_one", "status": "running"},
                {"host": "worker-two", "taskId": "host_worker_two", "status": "running"},
            ],
        )
        self.assertEqual(start_mock.call_count, 2)

    def test_create_reports_partial_host_start_failure(self):
        def start(prompt, account, host):
            if host == "offline-host":
                raise server.TaskStartError("host is offline")
            return "ses_worker01"

        with mock.patch.object(server, "start_host_task", side_effect=start):
            with self.request(
                "/v1/tasks",
                {"prompt": "작업 확인", "hosts": ["worker", "offline-host"]},
            ) as response:
                body = json.loads(response.read())
                status = response.status
        self.assertEqual(status, 202)
        self.assertEqual(body["tasks"][0]["status"], "running")
        self.assertEqual(body["tasks"][1]["status"], "failed")
        self.assertEqual(body["tasks"][1]["host"], "offline-host")

    def test_create_rejects_duplicate_hosts_before_starting(self):
        with mock.patch.object(server, "start_host_task") as start:
            with self.assertRaises(urllib.error.HTTPError) as context:
                self.request(
                    "/v1/tasks",
                    {"prompt": "작업 확인", "hosts": ["worker", "worker"]},
                )
        self.assertEqual(context.exception.code, 400)
        start.assert_not_called()

    def test_get_reads_aside_session_state(self):
        with self.temporary_state() as database:
            self.insert_session(
                database,
                "ses_done01",
                "idle",
                finished_at=10,
                result="서울은 맑습니다.",
            )
            with mock.patch.object(server, "state_db_path", return_value=database):
                with self.request("/v1/tasks/ses_done01") as response:
                    body = json.loads(response.read())
        self.assertEqual(body["taskId"], "ses_done01")
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["result"], "서울은 맑습니다.")
        self.assertEqual(body["raw"], "서울은 맑습니다.")

    def test_get_extracts_final_text_and_keeps_raw_message(self):
        raw = json.dumps(
            [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "2026년 9월 27일 (일) 저녁 7시 30분 KST."}],
                }
            ],
            ensure_ascii=False,
        )
        with self.temporary_state() as database:
            self.insert_session(database, "ses_raw001", "idle", finished_at=10, result=raw)
            with mock.patch.object(server, "state_db_path", return_value=database):
                with self.request("/v1/tasks/ses_raw001") as response:
                    body = json.loads(response.read())
        self.assertEqual(body["result"], "2026년 9월 27일 (일) 저녁 7시 30분 KST.")
        self.assertEqual(body["raw"], raw)

    def test_get_returns_host_task_state_without_reading_aside_db(self):
        task_id = "host_remote01"
        state = {
            "taskId": task_id,
            "status": "failed",
            "asideStatus": "errored",
            "host": "gs-aside-worker01",
            "error": "Model not found",
        }
        with mock.patch.dict(server.host_tasks, {task_id: state}):
            with self.request(f"/v1/tasks/{task_id}") as response:
                body = json.loads(response.read())
        self.assertEqual(body, state)

    def test_missing_task_returns_not_found(self):
        with self.temporary_state() as database:
            with mock.patch.object(server, "state_db_path", return_value=database):
                with self.assertRaises(urllib.error.HTTPError) as context:
                    self.request("/v1/tasks/missing01")
        self.assertEqual(context.exception.code, 404)

    def test_failed_session_exposes_error(self):
        with self.temporary_state() as database:
            self.insert_session(
                database,
                "ses_fail01",
                "errored",
                finished_at=10,
                abort_reason='{"kind":"error","error":"browser closed"}',
            )
            with mock.patch.object(server, "state_db_path", return_value=database):
                with self.request("/v1/tasks/ses_fail01") as response:
                    body = json.loads(response.read())
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "browser closed")

    def test_tasks_require_bearer_when_configured(self):
        with mock.patch.object(server, "MCP_BEARER_TOKEN", "secret"):
            request = urllib.request.Request(self.base_url + "/v1/tasks/ses_done01")
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(request, timeout=5)
        self.assertEqual(context.exception.code, 401)

    def test_list_hosts_returns_aside_host_inventory(self):
        inventory = {
            "defaultHost": "local",
            "hosts": [{"id": "remote-id", "deviceName": "worker", "online": True}],
        }
        with mock.patch.object(server, "list_aside_hosts", return_value=inventory) as list_hosts:
            with self.request("/v1/hosts?account=u1") as response:
                body = json.loads(response.read())
        self.assertEqual(body, inventory)
        list_hosts.assert_called_once_with("u1")

    def test_host_inventory_requires_bearer_when_configured(self):
        with mock.patch.object(server, "MCP_BEARER_TOKEN", "secret"):
            request = urllib.request.Request(self.base_url + "/v1/hosts")
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(request, timeout=5)
        self.assertEqual(context.exception.code, 401)

    def temporary_state(self):
        return TemporaryStateDb()

    def insert_session(self, database, task_id, status, finished_at=None, result=None, abort_reason=None):
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "INSERT INTO sessions (id, status, cwd, updated_at) VALUES (?, ?, ?, ?)",
                (task_id, status, "/", 20),
            )
            connection.execute(
                """
                INSERT INTO session_turns (
                    session_id, user_message, final_assistant_message, token_usage,
                    started_at, last_message_timestamp, finished_at, abort_reason,
                    jsonl_read_offset, jsonl_read_size
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    "prompt",
                    result,
                    "{}",
                    1,
                    2,
                    finished_at,
                    abort_reason,
                    0,
                    0,
                ),
            )
            connection.commit()
        finally:
            connection.close()


class TemporaryStateDb:
    def __enter__(self):
        self.directory = Path("/tmp") / f"aside-task-test-{threading.get_ident()}"
        self.directory.mkdir(exist_ok=True)
        self.database = self.directory / "state.db"
        connection = sqlite3.connect(self.database)
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY NOT NULL,
                status TEXT NOT NULL,
                cwd TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE session_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                user_message TEXT NOT NULL,
                final_assistant_message TEXT,
                token_usage TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                last_message_timestamp INTEGER NOT NULL,
                finished_at INTEGER,
                aborted_at INTEGER,
                abort_reason TEXT,
                jsonl_read_offset INTEGER NOT NULL,
                jsonl_read_size INTEGER NOT NULL
            );
            """
        )
        connection.commit()
        connection.close()
        return self.database

    def __exit__(self, exc_type, exc, traceback):
        self.database.unlink(missing_ok=True)
        self.directory.rmdir()


class AsideCommandTest(unittest.TestCase):
    def test_start_aside_exec_passes_account_option(self):
        process = mock.Mock()
        process.stderr = io.StringIO("created new session: ses_remote01\n")
        process.poll.return_value = None
        with mock.patch.object(server, "_drain_task_process"), mock.patch.object(
            server.subprocess, "Popen", return_value=process
        ) as popen:
            task_id = server.start_aside_exec("로컬에서 실행", "u1")
        self.assertEqual(task_id, "ses_remote01")
        command = popen.call_args.args[0]
        self.assertEqual(
            command,
            [server.aside_command(), "--account", "u1", "exec", "로컬에서 실행"],
        )

    def test_aside_mcp_session_passes_host_and_account_options(self):
        process = mock.Mock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO("")
        process.poll.return_value = 0
        with mock.patch.object(server.subprocess, "Popen", return_value=process) as popen:
            server.AsideMcpSession("u1", "gs-aside-worker01")
        self.assertEqual(
            popen.call_args.args[0],
            [
                server.aside_command(),
                "mcp",
                "--account",
                "u1",
                "--host",
                "gs-aside-worker01",
            ],
        )

    def test_list_aside_hosts_uses_json_cli(self):
        payload = {"defaultHost": "local", "hosts": []}
        completed = mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")
        with mock.patch.object(server.subprocess, "run", return_value=completed) as run:
            result = server.list_aside_hosts("u1")
        self.assertEqual(result, payload)
        self.assertEqual(
            run.call_args.args[0],
            [server.aside_command(), "host", "list", "--json", "--account", "u1"],
        )

    def test_host_task_stores_final_mcp_result(self):
        task_id = "host_remote01"
        state = {"taskId": task_id, "status": "running", "asideStatus": "running", "host": "worker"}
        with mock.patch.dict(
            server.host_tasks,
            {task_id: state},
        ), mock.patch.object(server, "save_host_task"):
            with mock.patch.object(server, "call_aside_exec", return_value="최종 응답"):
                server._run_host_task(task_id, "요청", "u1", "worker")
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["result"], "최종 응답")
        self.assertEqual(state["raw"], "최종 응답")

    def test_host_task_stores_mcp_failure(self):
        task_id = "host_remote02"
        state = {"taskId": task_id, "status": "running", "asideStatus": "running", "host": "worker"}
        with mock.patch.dict(server.host_tasks, {task_id: state}), mock.patch.object(
            server, "save_host_task"
        ):
            with mock.patch.object(server, "call_aside_exec", side_effect=RuntimeError("host offline")):
                server._run_host_task(task_id, "요청", None, "worker")
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], "host offline")

    def test_host_task_store_restores_result_and_marks_running_interrupted(self):
        completed_id = "host_completed01"
        running_id = "host_running001"
        completed = {
            "taskId": completed_id,
            "status": "succeeded",
            "asideStatus": "idle",
            "host": "worker",
            "raw": "완료 결과",
            "result": "완료 결과",
        }
        running = {
            "taskId": running_id,
            "status": "running",
            "asideStatus": "running",
            "host": "worker",
        }
        with TemporaryStateDb() as temporary_state:
            store = temporary_state.parent / "host-tasks.db"
            with mock.patch.object(server, "HOST_TASK_STATE_DB", store):
                server.save_host_task(completed)
                server.save_host_task(running)
                server.initialize_host_task_store()
                with mock.patch.dict(server.host_tasks, {}, clear=True):
                    self.assertEqual(server.read_host_task(completed_id), completed)
                    interrupted = server.read_host_task(running_id)
            store.unlink(missing_ok=True)
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertIn("재시작", interrupted["error"])


class McpRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        with server.sessions_lock:
            server.sessions.clear()

    def tearDown(self):
        with server.sessions_lock:
            session_ids = list(server.sessions)
        for session_id in session_ids:
            server.close_session(session_id)

    def request(self, path, method, payload=None, headers=None):
        data = None
        request_headers = dict(headers or {})
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=request_headers,
            method=method,
        )
        return urllib.request.urlopen(request, timeout=5)

    @staticmethod
    def make_session(host):
        session = mock.Mock()
        session.host = host
        session.request.side_effect = lambda message: {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "result": {"ok": True},
        }
        return session

    def test_legacy_mcp_path_keeps_session_local(self):
        session = self.make_session(None)
        with mock.patch.object(server, "AsideMcpSession", return_value=session) as create:
            with self.request(
                "/mcp",
                "POST",
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            ) as response:
                session_id = response.headers["Mcp-Session-Id"]
            with self.request(
                "/mcp",
                "POST",
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"Mcp-Session-Id": session_id},
            ) as response:
                body = json.loads(response.read())

        create.assert_called_once_with(host=None)
        self.assertIsNone(session.host)
        self.assertEqual(body["id"], 2)

    def test_host_route_keeps_requests_and_delete_on_selected_host(self):
        host = "gs-aside-worker01"
        path = f"/{host}/mcp"
        session = self.make_session(host)
        with mock.patch.object(server, "AsideMcpSession", return_value=session) as create:
            with self.request(
                path,
                "POST",
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            ) as response:
                session_id = response.headers["Mcp-Session-Id"]

            with self.request(
                path,
                "POST",
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"Mcp-Session-Id": session_id},
            ) as response:
                self.assertEqual(response.status, 202)

            with self.request(
                path,
                "POST",
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"Mcp-Session-Id": session_id},
            ) as response:
                self.assertEqual(json.loads(response.read())["id"], 2)

            with self.assertRaises(urllib.error.HTTPError) as context:
                self.request(
                    "/mcp",
                    "POST",
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                    {"Mcp-Session-Id": session_id},
                )
            self.assertEqual(context.exception.code, 400)

            with self.assertRaises(urllib.error.HTTPError) as context:
                self.request(
                    "/another-host/mcp",
                    "DELETE",
                    headers={"Mcp-Session-Id": session_id},
                )
            self.assertEqual(context.exception.code, 400)
            self.assertIn(session_id, server.sessions)

            with self.request(
                path,
                "DELETE",
                headers={"Mcp-Session-Id": session_id},
            ) as response:
                self.assertEqual(response.status, 204)

        create.assert_called_once_with(host=host)
        session.notify.assert_called_once_with(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        session.close.assert_called_once_with()

    def test_host_route_supports_options_and_rejects_encoded_slash(self):
        with self.request("/worker-one/mcp", "OPTIONS") as response:
            self.assertEqual(response.status, 204)
        self.assertEqual(server.mcp_route("/worker%2Ftwo/mcp"), (False, None))
        self.assertEqual(server.mcp_route("/worker/child/mcp"), (False, None))


if __name__ == "__main__":
    unittest.main()
