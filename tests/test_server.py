# -*- coding: utf-8 -*-
"""MCP 协议层用例。

分两层：

* **进程内** —— 直接驱动 :class:`McpServer`，覆盖协议分发与错误码，快且稳
* **跨进程** —— 真起一个 ``python -m yearning2_mcp`` 子进程，验证 stdio 分帧、
  stdout 纯净度与退出行为。这一层才能抓到「某处多了个 print」这类问题。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import shutil

from support import DEFAULT_PASSWORD, DEFAULT_USER, YearningTestCase
from yearning2_mcp import __version__, _log
from yearning2_mcp.client import YearningClient
from yearning2_mcp.server import McpServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT, 'src')
TOOL_COUNT = 14


def make_client(session_path, url):
    return YearningClient(
        {'endpoint': url, 'username': DEFAULT_USER,
         'password': DEFAULT_PASSWORD, 'login_mode': 'ldap'},
        session_path=session_path, timeout=5)


class InProcessProtocolTest(YearningTestCase):

    def setUp(self):
        super().setUp()
        self.sent = []
        client = make_client(self.session_path, self.url)
        self.server = McpServer(client, 'test-config', version=__version__,
                                send=self.sent.append)

    def rpc(self, method, params=None, rid=1):
        msg = {'jsonrpc': '2.0', 'id': rid, 'method': method}
        if params is not None:
            msg['params'] = params
        self.server.handle(msg)
        return self.sent[-1] if self.sent else None

    def notify(self, method, params=None):
        msg = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            msg['params'] = params
        self.server.handle(msg)

    # -------------------------------------------------------------- 握手

    def test_initialize_echoes_protocol_and_advertises_tools(self):
        resp = self.rpc('initialize', {'protocolVersion': '2025-06-18',
                                       'clientInfo': {'name': 'test'}})
        result = resp['result']
        self.assertEqual(result['protocolVersion'], '2025-06-18')
        self.assertEqual(result['serverInfo']['name'], 'yearning2-mcp')
        self.assertEqual(result['serverInfo']['version'], __version__)
        self.assertIn('tools', result['capabilities'])
        self.assertTrue(result['instructions'])

    def test_initialize_without_protocol_version_uses_default(self):
        resp = self.rpc('initialize', {})
        self.assertEqual(resp['result']['protocolVersion'], '2024-11-05')

    def test_initialized_notification_produces_no_response(self):
        self.notify('notifications/initialized')
        self.assertEqual(self.sent, [])

    def test_ping(self):
        self.assertEqual(self.rpc('ping')['result'], {})

    # -------------------------------------------------------------- 工具

    def test_tools_list(self):
        tools = self.rpc('tools/list')['result']['tools']
        self.assertEqual(len(tools), TOOL_COUNT)
        names = [t['name'] for t in tools]
        self.assertIn('yearning_status', names)
        for tool in tools:
            self.assertTrue(tool['description'])
            self.assertEqual(tool['inputSchema']['type'], 'object')

    def test_tools_list_has_no_handler_leak(self):
        for tool in self.rpc('tools/list')['result']['tools']:
            self.assertNotIn('handler', tool)

    def test_tools_call_success(self):
        resp = self.rpc('tools/call', {'name': 'yearning_system_stats',
                                       'arguments': {}})
        result = resp['result']
        self.assertFalse(result['isError'])
        self.assertEqual(result['content'][0]['type'], 'text')
        self.assertIn('工单总数 : 42', result['content'][0]['text'])

    def test_tools_call_unknown_tool_is_error(self):
        resp = self.rpc('tools/call', {'name': 'nope', 'arguments': {}})
        self.assertTrue(resp['result']['isError'])
        self.assertIn('未知工具', resp['result']['content'][0]['text'])

    def test_tools_call_blocked_path_is_error(self):
        resp = self.rpc('tools/call', {
            'name': 'yearning_api_get',
            'arguments': {'path': '/api/v2/query/results'}})
        result = resp['result']
        self.assertTrue(result['isError'])
        self.assertIn('拒绝', result['content'][0]['text'])

    def test_tools_call_without_arguments_key(self):
        resp = self.rpc('tools/call', {'name': 'yearning_board'})
        self.assertFalse(resp['result']['isError'])

    # -------------------------------------------------------------- 其它

    def test_resources_and_prompts_return_empty_lists(self):
        self.assertEqual(self.rpc('resources/list')['result'], {'resources': []})
        self.assertEqual(self.rpc('prompts/list')['result'], {'prompts': []})

    def test_unknown_method_returns_32601(self):
        resp = self.rpc('no/such/method')
        self.assertEqual(resp['error']['code'], -32601)

    def test_unknown_notification_is_silently_ignored(self):
        self.notify('no/such/notification')
        self.assertEqual(self.sent, [])

    def test_id_is_echoed_as_given(self):
        resp = self.rpc('ping', rid='abc-123')
        self.assertEqual(resp['id'], 'abc-123')

    def test_malformed_message_does_not_crash(self):
        self.server.handle(None)
        self.server.handle('not-a-dict')
        self.server.handle({})
        self.assertEqual(self.sent, [])


class StdioEndToEndTest(YearningTestCase):
    """真起子进程，走 stdio。"""

    def _env(self):
        env = dict(os.environ)
        existing = env.get('PYTHONPATH', '')
        env['PYTHONPATH'] = SRC_DIR + (os.pathsep + existing if existing else '')
        env['YEARNING_CONFIG'] = self.config_path
        env['YEARNING_SESSION'] = self.session_path
        env['YEARNING_MCP_LOG'] = self.log_path
        env['PYTHONUTF8'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        return env

    def _spawn(self):
        proc = subprocess.Popen(
            [sys.executable, '-m', 'yearning2_mcp'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=self._env(), cwd=ROOT)
        self.addCleanup(self._kill, proc)
        return proc

    @staticmethod
    def _kill(proc):
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    def _send(self, proc, obj):
        proc.stdin.write((json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8'))
        proc.stdin.flush()

    def _read(self, proc, count, timeout=30):
        """按行读 count 条 JSON，返回 (消息列表, 原始行列表)。"""
        messages, raw_lines = [], []
        deadline = time.time() + timeout
        while len(messages) < count and time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            raw_lines.append(line)
            text = line.decode('utf-8').strip()
            if not text:
                continue
            messages.append(json.loads(text))   # 不是合法 JSON 就该炸
        return messages, raw_lines

    def test_full_stdio_handshake_and_tool_call(self):
        proc = self._spawn()
        self._send(proc, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                          'params': {'protocolVersion': '2024-11-05',
                                     'capabilities': {},
                                     'clientInfo': {'name': 'e2e-test',
                                                    'version': '1'}}})
        messages, raw = self._read(proc, 1)
        self.assertEqual(len(messages), 1, '没收到 initialize 响应')
        init = messages[0]['result']
        self.assertEqual(init['serverInfo']['name'], 'yearning2-mcp')

        self._send(proc, {'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self._send(proc, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        self._send(proc, {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                          'params': {'name': 'yearning_status',
                                     'arguments': {}}})

        messages, raw = self._read(proc, 2)
        by_id = dict((m['id'], m) for m in messages if 'id' in m)
        self.assertEqual(len(by_id[2]['result']['tools']), TOOL_COUNT)
        status_text = by_id[3]['result']['content'][0]['text']
        self.assertIn('账号     : tester', status_text)
        self.assertIn('真实姓名 : 测试用户', status_text)

        # stdout 必须只有协议报文，一条不许多
        for line in raw:
            self.assertTrue(line.decode('utf-8').strip().startswith('{'),
                            'stdout 混入了非协议内容: %r' % line)
            self.assertNotIn(b'\r\n', line, 'stdout 不该出现 CRLF')

        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=15), 0)
        self.assertEqual(proc.stderr.read(), b'', 'stderr 应当干净')

    def test_stdio_blocks_dangerous_path(self):
        proc = self._spawn()
        self._send(proc, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                          'params': {}})
        self._read(proc, 1)
        self._send(proc, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                          'params': {'name': 'yearning_api_get',
                                     'arguments': {'path': '/api/v2/query/results'}}})
        messages, _raw = self._read(proc, 1)
        result = messages[0]['result']
        self.assertTrue(result['isError'])
        self.assertIn('拒绝', result['content'][0]['text'])
        self.assertNotIn('/api/v2/query/results', self.fake.paths(),
                         '被拦截的路径绝不能发出去')

    def test_malformed_line_is_skipped_not_fatal(self):
        proc = self._spawn()
        proc.stdin.write(b'this is not json\n')
        proc.stdin.flush()
        self._send(proc, {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        messages, _raw = self._read(proc, 1)
        self.assertEqual(messages[0]['id'], 1)

    def test_log_file_written_and_usable(self):
        proc = self._spawn()
        self._send(proc, {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        self._read(proc, 1)
        proc.stdin.close()
        proc.wait(timeout=15)
        self.assertTrue(os.path.exists(self.log_path))
        content = open(self.log_path, encoding='utf-8').read()
        self.assertIn('server start', content)
        self.assertIn('server exit', content)


class CliTest(YearningTestCase):
    """CLI 子命令 —— 都不该启动服务。"""

    def _run(self, *args, **kwargs):
        config_env = kwargs.pop('config_env', self.config_path)
        env = dict(os.environ)
        existing = env.get('PYTHONPATH', '')
        env['PYTHONPATH'] = SRC_DIR + (os.pathsep + existing if existing else '')
        env['YEARNING_CONFIG'] = config_env
        env['YEARNING_SESSION'] = self.session_path
        env['YEARNING_MCP_LOG'] = self.log_path
        env['PYTHONUTF8'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        proc = subprocess.Popen(
            [sys.executable, '-m', 'yearning2_mcp'] + list(args),
            # stdin 必须显式 DEVNULL：不指定的话子进程会继承测试进程的 stdin，
            # 万一走到 stdio 服务分支就会一直等输入，把整个测试挂死。
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=ROOT, **kwargs)
        out, err = proc.communicate(timeout=60)
        return (proc.returncode, out.decode('utf-8', 'replace'),
                err.decode('utf-8', 'replace'))

    def test_version(self):
        code, out, _err = self._run('--version')
        self.assertEqual(code, 0)
        self.assertIn(__version__, out)

    def test_check_config_hides_password(self):
        code, out, _err = self._run('--check-config')
        self.assertEqual(code, 0)
        self.assertIn('目标实例', out)
        self.assertIn(DEFAULT_USER, out)
        self.assertNotIn(DEFAULT_PASSWORD, out, '--check-config 不能回显密码')

    def test_print_tools_outputs_valid_json(self):
        code, out, _err = self._run('--print-tools')
        self.assertEqual(code, 0)
        tools = json.loads(out)
        self.assertEqual(len(tools), TOOL_COUNT)
        self.assertEqual(sorted(tools[0]), ['description', 'inputSchema', 'name'])

    def test_selftest_passes_against_fake(self):
        report = os.path.join(self.tmp, 'report.txt')
        code, out, _err = self._run('--selftest', '--report', report)
        self.assertEqual(code, 0, out)
        self.assertIn('[PASS] 配置载入', out)
        self.assertIn('[PASS] TCP 连通性', out)
        self.assertIn('[PASS] 登录认证', out)
        self.assertIn('[PASS] 接口拦截规则', out)
        self.assertIn('连接正常', out)
        self.assertTrue(os.path.exists(report))
        self.assertNotIn(DEFAULT_PASSWORD, open(report, encoding='utf-8').read())

    def test_selftest_fails_cleanly_on_dead_endpoint(self):
        self.write_config(endpoint='http://127.0.0.1:9')
        code, out, _err = self._run('--selftest')
        self.assertEqual(code, 1)
        self.assertIn('[FAIL] TCP 连通性', out)

    def test_missing_config_exits_nonzero_on_stdout_safe_channel(self):
        missing = os.path.join(self.tmp, 'missing.json')
        code, out, err = self._run(config_env=missing)
        self.assertEqual(code, 2)
        self.assertIn('yearning2-mcp 启动失败', err)
        self.assertEqual(out, '', '失败时 stdout 必须干净')


class LogTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='yearning2-mcp-log-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, 'mcp.log')
        self.addCleanup(self._reset)
        _log.set_log_file(self.path)

    @staticmethod
    def _reset():
        _log._log_resolved = False      # 让后续测试重新按环境变量解析
        _log._log_file = None

    def test_log_appends_with_timestamp(self):
        _log.log('hello')
        content = open(self.path, encoding='utf-8').read()
        self.assertIn('hello', content)
        self.assertRegex(content, r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}')

    def test_stdout_trap_redirects_to_log(self):
        trap = _log.StdoutTrap()
        trap.write('someone printed this\n')
        trap.flush()
        content = open(self.path, encoding='utf-8').read()
        self.assertIn('stdout-trap', content)
        self.assertIn('someone printed this', content)

    def test_trap_ignores_blank_writes(self):
        trap = _log.StdoutTrap()
        trap.write('')
        trap.write('   \n')
        self.assertFalse(os.path.exists(self.path))

    def test_log_disabled_is_noop(self):
        _log.set_log_file(None)
        _log.log('nope')
        self.assertFalse(os.path.exists(self.path))

    def test_install_stdout_trap_swaps_stream(self):
        original = sys.stdout
        try:
            _log.install_stdout_trap()
            self.assertIsInstance(sys.stdout, _log.StdoutTrap)
        finally:
            sys.stdout = original


if __name__ == '__main__':
    unittest.main()
