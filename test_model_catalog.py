import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import server
import test_server


class ModelCatalogTest(unittest.TestCase):
    setUpClass = classmethod(test_server.OpenAIAdapterTest.setUpClass.__func__)
    tearDownClass = classmethod(test_server.OpenAIAdapterTest.tearDownClass.__func__)
    request = test_server.OpenAIAdapterTest.request

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        patch = mock.patch.dict(os.environ, {'ASIDE_HOME': self.temp.name})
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, name, data):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def test_endpoint_lists_current_account_models_without_secrets(self):
        self.write('accounts.json', {'currentAccountId': 1})
        self.write('u/0/models.json', {'providers': {'other': {'models': [{'id': 'private'}]}}})
        self.write('u/1/models.json', {'providers': {
            'custom': {'apiKey': 'SECRET', 'baseUrl': 'PRIVATE_URL', 'models': [
                {'id': 'nested/model', 'name': 'Friendly', 'apiKey': 'SECRET'},
                {'id': 'nested/model', 'name': 'Friendly'},
                {'id': 'embed', 'type': 'embedding'},
            ]},
            'oauth': {'accountModelCatalog': {'modelIds': ['allowed']}}
        }})
        self.write('u/1/cache/models-catalog.json', {'oauth': {'models': [
            {'id': 'allowed'}, {'id': 'unavailable'}]}})
        with self.request('/v1/models') as response:
            text = response.read().decode()
            models = json.loads(text)['data']
        self.assertEqual([x['id'] for x in models],
                         [server.OPENAI_MODEL, 'custom/nested/model', 'oauth/allowed'])
        self.assertNotIn('SECRET', text)
        self.assertNotIn('PRIVATE_URL', text)
        self.assertEqual(models[1]['name'], 'Friendly')

    def test_config_change_is_visible_without_restart(self):
        self.write('u/0/models.json', {'providers': {'custom': {'models': [{'id': 'first'}]}}})
        self.assertEqual(server.available_models()[-1]['id'], 'custom/first')
        self.write('u/0/models.json', {'providers': {'custom': {'models': [{'id': 'second'}]}}})
        self.assertEqual(server.available_models()[-1]['id'], 'custom/second')

    def test_missing_or_invalid_catalog_keeps_default_alias(self):
        self.assertEqual([m['id'] for m in server.available_models()], [server.OPENAI_MODEL])
        self.write('u/0/models.json', {})
        (self.home / 'u/0/models.json').write_text('{')
        self.assertEqual([m['id'] for m in server.available_models()], [server.OPENAI_MODEL])
