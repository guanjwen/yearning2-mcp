# -*- coding: utf-8 -*-
"""配置载入的用例。"""
import json
import os
import shutil
import tempfile
import unittest

from yearning2_mcp import config


class NormalizeEndpointTest(unittest.TestCase):

    def test_adds_scheme(self):
        self.assertEqual(config.normalize_endpoint('yearning.corp:8000'),
                         'http://yearning.corp:8000')

    def test_strips_trailing_slash(self):
        self.assertEqual(config.normalize_endpoint('http://a.b:8000/'),
                         'http://a.b:8000')

    def test_keeps_https(self):
        self.assertEqual(config.normalize_endpoint('https://a.b/'),
                         'https://a.b')

    def test_empty(self):
        self.assertEqual(config.normalize_endpoint(None), '')


class LoadConfigTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='yearning2-mcp-cfg-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, 'config.json')
        keys = [config.ENV_ENDPOINT, config.ENV_USERNAME, config.ENV_PASSWORD,
                config.ENV_LOGIN_MODE, config.ENV_CONFIG]
        self._saved = dict((k, os.environ.get(k)) for k in keys)
        for key in keys:
            os.environ.pop(key, None)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write(self, data):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def test_missing_required_raises(self):
        self._write({'endpoint': 'http://a.b'})
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config(explicit=self.path)
        self.assertIn('username', str(ctx.exception))
        self.assertIn('password', str(ctx.exception))

    def test_explicit_missing_file_raises(self):
        with self.assertRaises(config.ConfigError):
            config.load_config(explicit=os.path.join(self.tmp, 'nope.json'))

    def test_env_overrides_file(self):
        self._write({'endpoint': 'http://from-file',
                     'username': 'file-user', 'password': 'file-pass'})
        os.environ[config.ENV_USERNAME] = 'env-user'
        cfg, source = config.load_config(explicit=self.path)
        self.assertEqual(cfg['username'], 'env-user')
        self.assertEqual(cfg['password'], 'file-pass')
        self.assertIn('env:', source)

    def test_login_mode_defaults_to_ldap(self):
        self._write({'endpoint': 'http://a.b', 'username': 'u',
                     'password': 'p', 'login_mode': 'whatever'})
        cfg, _ = config.load_config(explicit=self.path)
        self.assertEqual(cfg['login_mode'], 'ldap')

    def test_require_false_allows_partial(self):
        self._write({'endpoint': 'http://a.b'})
        cfg, _ = config.load_config(explicit=self.path, require=False)
        self.assertEqual(cfg['endpoint'], 'http://a.b')

    def test_invalid_json_raises(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('{not json')
        with self.assertRaises(config.ConfigError):
            config.load_config(explicit=self.path)

    def test_non_object_json_raises(self):
        self._write(['a', 'b'])
        with self.assertRaises(config.ConfigError):
            config.load_config(explicit=self.path)

    def test_describe_never_leaks_password(self):
        self._write({'endpoint': 'http://a.b', 'username': 'u',
                     'password': 'super-secret-value'})
        cfg, source = config.load_config(explicit=self.path)
        text = config.describe(cfg, source)
        self.assertNotIn('super-secret-value', text)
        self.assertIn('已设置', text)


class MaskTest(unittest.TestCase):

    def test_mask_hides_middle(self):
        self.assertEqual(config.mask_secret('abcdef'), 'a****f')

    def test_mask_short(self):
        self.assertEqual(config.mask_secret('ab'), '**')
        self.assertEqual(config.mask_secret(''), '')


if __name__ == '__main__':
    unittest.main()
