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


if __name__ == "__main__":
    unittest.main()
