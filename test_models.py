import json
import subprocess
import unittest
from unittest import mock
import server
import test_server


class ModelSelectionTest(unittest.TestCase):
    setUpClass = classmethod(test_server.OpenAIAdapterTest.setUpClass.__func__)
    tearDownClass = classmethod(test_server.OpenAIAdapterTest.tearDownClass.__func__)
    request = test_server.OpenAIAdapterTest.request

    def test_selected_model_forwarded_and_echoed(self):
        for stream in (False, True):
            with mock.patch.object(server, 'call_aside_exec', return_value='MODEL_OK') as execute:
                with self.request('/v1/chat/completions', {
                    'model': 'provider/model', 'stream': stream,
                    'messages': [{'role': 'user', 'content': 'hello'}],
                }) as response:
                    body = response.read().decode()
            self.assertEqual(execute.call_args.args[1], 'provider/model')
            if stream:
                chunks = [json.loads(line[6:]) for line in body.splitlines()
                          if line.startswith('data: ') and line != 'data: [DONE]']
                self.assertTrue(all(c['model'] == 'provider/model' for c in chunks))
            else:
                self.assertEqual(json.loads(body)['model'], 'provider/model')

    def test_cli_argument_boundaries_and_output(self):
        result = subprocess.CompletedProcess([], 0, 'MODEL_OK\x1b[0m\n', '')
        with mock.patch.object(server.subprocess, 'run', return_value=result) as run:
            self.assertEqual(server.call_aside_exec('--prompt', 'provider/model'), 'MODEL_OK')
        self.assertEqual(run.call_args.args[0][1:],
                         ['exec', '--model', 'provider/model', '--', '--prompt'])
        self.assertEqual(run.call_args.kwargs['timeout'], server.MCP_REQUEST_TIMEOUT)

    def test_cli_zero_exit_error_not_success(self):
        result = subprocess.CompletedProcess([], 0, '\n • \x1b[31mError\x1b[0m unavailable', '')
        with mock.patch.object(server.subprocess, 'run', return_value=result):
            with self.assertRaises(RuntimeError):
                server.call_aside_exec('hello', 'provider/missing')

    def test_default_alias_uses_existing_mcp(self):
        self.assertIsNone(server.selected_model(server.OPENAI_MODEL))

    def test_cli_timeout_becomes_timeout_error(self):
        with mock.patch.object(server.subprocess, 'run', side_effect=subprocess.TimeoutExpired('aside', 1)):
            with self.assertRaises(TimeoutError):
                server.call_aside_exec('hello', 'provider/model')

    @unittest.skipUnless(hasattr(server, 'start_aside_exec'), 'Tasks API is a local extension')
    def test_task_model_forwarded(self):
        with mock.patch.object(server, 'start_aside_exec', return_value='ses_model') as start:
            with self.request('/v1/tasks', {'prompt': 'hello', 'model': 'provider/model'}) as response:
                self.assertEqual(response.status, 202)
        start.assert_called_once_with('hello', None, 'provider/model')
