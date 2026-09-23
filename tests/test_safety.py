# -*- coding: utf-8 -*-
"""接口安全策略的用例 —— 表驱动，不依赖网络。"""
import unittest

from yearning2_mcp import safety


class CheckPathTest(unittest.TestCase):

    ALLOWED = [
        '/api/v2/dash/count',
        '/api/v2/fetch/idc',
        '/api/v2/dash/pie',
        '/api/v2/fetch/detail?page=1&pagesize=20',
        '/api/v2/manage/group',          # 前缀黑名单里的只读例外
        '/api/v2/query/fetch_table',     # 只读的表结构查询
    ]

    DENIED = [
        '',
        '/api/v2/query/results',
        '/api/v2/query/results/',
        '/api/v2/query/results?data_base=x',
        '/API/V2/QUERY/RESULTS',                     # 大小写变体
        '/api/v2/fetch/perform',                     # 敏感：密码哈希
        '/api/v2/fetch/perform/',
        '/api/v2/audit/order/1',                     # 审批域
        '/api/v2/manage/user',
        '/api/v2/manage/db',
        '/api/v2/manage/setting',
        '/api/v2/fetch/merge',
        '/api/v2/fetch/marge',
        '/api/v2/fetch/roll_order',
        '/api/v2/fetch/test',
        '/api/v2/../../etc/passwd',                  # 裸穿越
        '/api/v2/%2e%2e/%2e%2e/admin',               # 编码穿越
        'http://evil.example/api/v2/dash/count',     # 绝对 URL
        '//evil.example/api/v2/dash/count',
        '/other/prefix',
        'api/v2/dash/count',                         # 缺前导斜杠
    ]

    def test_allowed_paths(self):
        for path in self.ALLOWED:
            with self.subTest(path=path):
                self.assertIsNone(safety.check_get_path(path),
                                  '应当放行: %s' % path)

    def test_denied_paths(self):
        for path in self.DENIED:
            with self.subTest(path=path):
                reason = safety.check_get_path(path)
                self.assertIsNotNone(reason, '应当拒绝: %s' % path)
                self.assertTrue(reason.startswith('拒绝'),
                                '拒绝原因应以「拒绝」开头: %r' % reason)

    def test_perform_reason_mentions_password(self):
        reason = safety.check_get_path('/api/v2/fetch/perform')
        self.assertIn('密码哈希', reason)

    def test_results_reason_mentions_sql(self):
        reason = safety.check_get_path('/api/v2/query/results')
        self.assertIn('SQL', reason)

    def test_blocked_summary_shape(self):
        summary = safety.blocked_summary()
        self.assertIn('/api/v2/fetch/test', summary['exact'])
        self.assertIn('/api/v2/fetch/perform', summary['sensitive'])
        self.assertTrue(any(k.startswith('/api/v2/audit') for k in summary['prefix']))

    def test_write_endpoints_are_blocked_as_get(self):
        """写接口的 GET 形式也要挡 —— 否则透传工具看着像能调它们。"""
        for path in ('/api/v2/query/results', '/api/v2/query/refer',
                     '/api/v2/fetch/undo'):
            with self.subTest(path=path):
                reason = safety.check_get_path(path)
                self.assertIsNotNone(reason, 'GET %s 应当被拒' % path)
                self.assertIn('yearning_', reason, '拒绝原因里应指向专用工具')

    def test_normalize_strips_query_and_case(self):
        self.assertEqual(safety.normalize_path('/API/v2/Dash/Count/?a=1'),
                         '/api/v2/dash/count')


class CheckWriteTest(unittest.TestCase):
    """写动作白名单 —— 专用工具只能按这张表发请求。"""

    def test_all_actions_resolve(self):
        for action in safety.WRITE_ACTIONS:
            with self.subTest(action=action):
                method, path = safety.check_write(action)
                self.assertIn(method, ('GET', 'POST', 'PUT', 'DELETE'))
                self.assertTrue(path.startswith('/api/v2/'))

    def test_unknown_action_raises(self):
        with self.assertRaises(ValueError):
            safety.check_write('drop_database')

    def test_paths_are_hardcoded(self):
        """动作只能映射到预置路径 —— 调用方无法自定义（防 SSRF / 打偏）。"""
        mapping = dict((k, safety.check_write(k)[1])
                       for k in safety.WRITE_ACTIONS)
        self.assertEqual(mapping['submit_order'], '/api/v2/common/order')
        self.assertEqual(mapping['run_query'], '/api/v2/query/results')
        self.assertEqual(mapping['cancel_order'], '/api/v2/fetch/undo')
        self.assertEqual(mapping['end_query'], '/api/v2/query')

    def test_confirm_required_set(self):
        """提工单/撤销类必须两步确认；只读查询不需要。"""
        for action in ('submit_order', 'submit_query_order',
                       'cancel_order', 'end_query'):
            self.assertTrue(safety.needs_confirm(action), action)
        self.assertFalse(safety.needs_confirm('run_query'))
        self.assertFalse(safety.needs_confirm('my_orders'))

    def test_no_audit_or_manage_action_exposed(self):
        for action in safety.WRITE_ACTIONS:
            path = safety.check_write(action)[1]
            with self.subTest(action=action):
                self.assertFalse(path.startswith('/api/v2/audit/'))
                self.assertFalse(path.startswith('/api/v2/manage/'))

    def test_write_summary_shape(self):
        summary = safety.write_summary()
        self.assertEqual(set(summary), set(safety.WRITE_ACTIONS))
        self.assertIn('POST', summary['submit_order'])


if __name__ == '__main__':
    unittest.main()
