import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import server


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

    def test_unknown_model_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.request(
                "/v1/chat/completions",
                {
                    "model": "unknown",
                    "messages": [{"role": "user", "content": "안녕"}],
                },
            )
        self.assertEqual(context.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
