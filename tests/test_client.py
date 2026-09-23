# -*- coding: utf-8 -*-
"""HTTP 客户端用例，跑在假 Yearning 上（离线）。"""
import base64
import json
import os
import time
import unittest

from support import DEFAULT_PASSWORD, DEFAULT_USER, YearningTestCase
from yearning2_mcp.client import (ConnectionFailed, LoginFailed, YearningClient,
                                 default_session_path, jwt_claims)

DEAD_ENDPOINT = 'http://127.0.0.1:9'


class LoginTest(YearningTestCase):

    def make_client(self, config_path_used=None, **overrides):
        cfg = {'endpoint': self.url, 'username': DEFAULT_USER,
               'password': DEFAULT_PASSWORD, 'login_mode': 'ldap'}
        cfg.update(overrides)
        return YearningClient(cfg, session_path=self.session_path, timeout=5,
                              logger=lambda _m: None)

    def test_login_via_ldap(self):
        client = self.make_client()
        client.login()
        self.assertEqual(client.login_api, '/ldap')
        self.assertEqual(client.realname, '测试用户')
        self.assertEqual(client.claims.get('name'), 'tester')
        self.assertEqual(client.claims.get('role'), 'guest')
        self.assertTrue(client.token)
        self.assertIn('/ldap', [p for _m, p in self.fake.requests])

    def test_local_mode_tries_login_first(self):
        self.fake.local_enabled = True
        self.fake.ldap_enabled = False
        client = self.make_client(login_mode='local')
        client.login()
        self.assertEqual(client.login_api, '/login')

    def test_wrong_password_raises_and_hides_password(self):
        client = self.make_client(password='super-wrong-password')
        with self.assertRaises(LoginFailed) as ctx:
            client.login()
        text = str(ctx.exception)
        self.assertIn('登录失败', text)
        self.assertNotIn('super-wrong-password', text)
        # 也不能把它写进会话缓存
        if os.path.exists(self.session_path):
            self.assertNotIn('super-wrong-password',
                             open(self.session_path, encoding='utf-8').read())

    def test_unreachable_raises_connection_failed(self):
        client = YearningClient({'endpoint': DEAD_ENDPOINT, 'username': 'u',
                                 'password': 'p'},
                                session_path=self.session_path, timeout=3)
        with self.assertRaises(ConnectionFailed):
            client.login()

    def test_tcp_probe(self):
        client = self.make_client()
        self.assertTrue(client.tcp_ok()[0])
        dead = YearningClient({'endpoint': DEAD_ENDPOINT, 'username': 'u',
                               'password': 'p'},
                              session_path=self.session_path)
        reachable, err = dead.tcp_ok(timeout=3)
        self.assertFalse(reachable)
        self.assertTrue(err)

    def test_ensure_login_is_idempotent(self):
        client = self.make_client()
        client.ensure_login()
        before = len(self.fake.requests)
        client.ensure_login()
        self.assertEqual(len(self.fake.requests), before,
                         '已有 token 时不该重复登录')

    def test_token_seconds_left(self):
        client = self.make_client()
        client.login()
        self.assertGreater(client.token_seconds_left(), 7 * 3600)


class SessionCacheTest(YearningTestCase):

    def make_client(self, **overrides):
        cfg = {'endpoint': self.url, 'username': DEFAULT_USER,
               'password': DEFAULT_PASSWORD, 'login_mode': 'ldap'}
        cfg.update(overrides)
        return YearningClient(cfg, session_path=self.session_path, timeout=5)

    def _write_session(self, endpoint=None, token=None, extra=None):
        data = {'endpoint': endpoint or self.url, 'token': token or '',
                'realname': '缓存用户', 'permissions': 'guest',
                'login_api': '/ldap'}
        if extra:
            data.update(extra)
        with open(self.session_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)

    def test_reuses_token_and_restores_identity(self):
        first = self.make_client()
        first.login()
        calls_after_login = len(self.fake.requests)

        second = self.make_client()
        second.ensure_login()
        self.assertTrue(second.token)
        # 真实姓名只存在登录响应里，必须靠会话缓存补回来
        self.assertEqual(second.realname, '测试用户')
        self.assertTrue(second.login_api.endswith('(cached)'))
        self.assertEqual(len(self.fake.requests), calls_after_login,
                         '命中缓存不该再发登录请求')

    def test_ignores_cache_from_other_endpoint(self):
        self._write_session(endpoint='http://another-yearning:8000',
                            token='a.b.c')
        client = self.make_client()
        self.assertEqual(client.token, '')
        self.assertEqual(client.realname, '')

    def test_ignores_expired_token(self):
        payload = base64.urlsafe_b64encode(
            json.dumps({'exp': int(time.time()) - 10, 'name': 'old',
                        'role': 'guest'}).encode()).decode().rstrip('=')
        self._write_session(token='h.%s.s' % payload)
        client = self.make_client()
        self.assertEqual(client.token, '')

    def test_clear_session(self):
        first = self.make_client()
        first.login()
        first.clear_session()
        self.assertFalse(os.path.exists(self.session_path))
        self.assertEqual(first.token, '')


class HelperTest(unittest.TestCase):

    def test_default_session_path_env_and_default(self):
        saved = os.environ.get('YEARNING_SESSION')
        os.environ.pop('YEARNING_SESSION', None)
        try:
            self.assertTrue(default_session_path().endswith('session.json'))
            os.environ['YEARNING_SESSION'] = 'X:/tmp/x.json'
            self.assertEqual(default_session_path(), 'X:/tmp/x.json')
        finally:
            if saved is None:
                os.environ.pop('YEARNING_SESSION', None)
            else:
                os.environ['YEARNING_SESSION'] = saved

    def test_jwt_claims_garbage_is_safe(self):
        self.assertEqual(jwt_claims('not-a-jwt'), {})
        self.assertEqual(jwt_claims(''), {})


if __name__ == '__main__':
    unittest.main()
