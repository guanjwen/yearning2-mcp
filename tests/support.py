# -*- coding: utf-8 -*-
"""测试公共设施：起假服务、隔离环境变量与临时文件。

关键点是**绝不碰开发机真实的 ~/.yearning** —— 所有路径都改到临时目录。
"""
import json
import os
import shutil
import tempfile
import unittest

from fake_yearning import FakeYearning

ISOLATED_ENV = ('YEARNING_CONFIG', 'YEARNING_SESSION', 'YEARNING_MCP_LOG',
                'YEARNING_ENDPOINT', 'YEARNING_USERNAME', 'YEARNING_PASSWORD',
                'YEARNING_LOGIN_MODE')

DEFAULT_USER = 'tester'
DEFAULT_PASSWORD = 's3cret'


class YearningTestCase(unittest.TestCase):
    """所有用例的基类：假服务 + 干净环境 + 临时目录。"""

    def setUp(self):
        self.fake = FakeYearning(username=DEFAULT_USER,
                                 password=DEFAULT_PASSWORD)
        self.url = self.fake.start()
        self.addCleanup(self.fake.stop)

        self.tmp = tempfile.mkdtemp(prefix='yearning2-mcp-test-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.session_path = os.path.join(self.tmp, 'session.json')
        self.config_path = os.path.join(self.tmp, 'config.json')
        self.log_path = os.path.join(self.tmp, 'mcp.log')

        # 先把可能存在的同名环境变量挪开，避免污染测试
        for key in ISOLATED_ENV:
            self._set_env(key, None)

        self._set_env('YEARNING_CONFIG', self.config_path)
        self._set_env('YEARNING_SESSION', self.session_path)
        self._set_env('YEARNING_MCP_LOG', self.log_path)

        self.write_config()

    # -------------------------------------------------------------- 环境

    def _set_env(self, key, value):
        old = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

        def restore():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self.addCleanup(restore)

    # -------------------------------------------------------------- 配置

    def write_config(self, **overrides):
        cfg = {
            'endpoint': self.url,
            'username': DEFAULT_USER,
            'password': DEFAULT_PASSWORD,
            'login_mode': 'ldap',
        }
        cfg.update(overrides)
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False)
        return cfg
