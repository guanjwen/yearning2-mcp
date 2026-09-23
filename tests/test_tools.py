# -*- coding: utf-8 -*-
"""工具行为用例。

重点是验证「拒绝」不只是话术 —— 被闸门挡下的调用**真的没有发出请求**。
每个负向用例都断言假服务上没留下对应的请求痕迹，这样才不能靠改提示语蒙过去。
"""
import unittest

from support import DEFAULT_PASSWORD, DEFAULT_USER, YearningTestCase
from yearning2_mcp.client import YearningClient
from yearning2_mcp.tools import ToolError, build_tools, public_tools

ALL_TOOLS = {
    'yearning_status', 'yearning_allowed_sources',
    'yearning_system_stats', 'yearning_list_environments',
    'yearning_datasource_usage', 'yearning_board', 'yearning_orders',
    'yearning_my_orders', 'yearning_query_status', 'yearning_run_query',
    'yearning_submit_order', 'yearning_submit_query_order',
    'yearning_revoke', 'yearning_api_get',
}

READ_TOOLS = ('yearning_status', 'yearning_system_stats',
              'yearning_list_environments', 'yearning_datasource_usage',
              'yearning_board', 'yearning_orders', 'yearning_allowed_sources')


class ToolsTest(YearningTestCase):

    def setUp(self):
        super().setUp()
        self.client = YearningClient(
            {'endpoint': self.url, 'username': DEFAULT_USER,
             'password': DEFAULT_PASSWORD, 'login_mode': 'ldap'},
            session_path=self.session_path, timeout=5)
        self.tools = dict((t['name'], t)
                          for t in build_tools(self.client, 'test-config'))

    def call(self, name, **args):
        return self.tools[name]['handler'](args)

    def fresh(self, **fake_kwargs):
        """换一个配置不同的假服务（用于测各种闸门分支）。"""
        self.fake.stop()
        from fake_yearning import FakeYearning
        fake = FakeYearning(username=DEFAULT_USER, password=DEFAULT_PASSWORD,
                            **fake_kwargs)
        self.fake = fake
        self.url = fake.start()
        self.addCleanup(fake.stop)
        self.client = YearningClient(
            {'endpoint': self.url, 'username': DEFAULT_USER,
             'password': DEFAULT_PASSWORD, 'login_mode': 'ldap'},
            session_path=self.session_path, timeout=5)
        self.tools = dict((t['name'], t)
                          for t in build_tools(self.client, 'test-config'))

    # ------------------------------------------------------------- 工具清单

    def test_tool_names_and_schema(self):
        self.assertEqual(set(self.tools), ALL_TOOLS)
        for spec in public_tools(list(self.tools.values())):
            self.assertTrue(spec['description'])
            self.assertEqual(spec['inputSchema']['type'], 'object')

    def test_public_tools_hides_handler(self):
        for spec in public_tools(list(self.tools.values())):
            self.assertNotIn('handler', spec)

    def test_required_fields_declared(self):
        schema = self.tools['yearning_submit_order']['inputSchema']
        for key in ('idc', 'source', 'data_base', 'sql', 'type', 'text', 'assigned'):
            self.assertIn(key, schema['required'])

    # ------------------------------------------------------------- 只读工具

    def test_status(self):
        text = self.call('yearning_status')
        self.assertIn('TCP 连通 : 可达', text)
        self.assertIn('账号     : tester', text)
        self.assertIn('真实姓名 : 测试用户', text)
        # 真实权限来自 fetch/source，而不是误导性的 manage/group
        self.assertIn('env-demo-a', text)
        self.assertIn('ddl=1', text)
        self.assertIn('query=2', text)
        self.assertIn('env-demo-b', text)
        self.assertIn('不代表没有权限', text)

    def test_allowed_sources(self):
        text = self.call('yearning_allowed_sources', idc='env-demo-a', type='ddl')
        self.assertIn('demo_db_src', text)
        self.assertIn('auditor-a', text)

    def test_allowed_sources_flags_whitespace(self):
        """带前导空格的脏数据源名要标出来，不然用户复制过去会对不上。"""
        text = self.call('yearning_allowed_sources', idc='env-demo-a', type='query')
        self.assertIn('db-readonly', text)
        self.assertIn('前后空格', text)

    def test_allowed_sources_empty_env(self):
        text = self.call('yearning_allowed_sources', idc='env-demo-b', type='ddl')
        self.assertIn('（空', text)

    def test_allowed_sources_requires_idc(self):
        with self.assertRaises(ToolError):
            self.call('yearning_allowed_sources')

    def test_allowed_sources_rejects_bad_type(self):
        with self.assertRaises(ToolError):
            self.call('yearning_allowed_sources', idc='env-demo-a', type='truncate')

    def test_system_stats(self):
        text = self.call('yearning_system_stats')
        self.assertIn('用户数   : 3', text)
        self.assertIn('工单总数 : 42', text)

    def test_list_environments(self):
        text = self.call('yearning_list_environments')
        self.assertIn('env-demo-a', text)
        self.assertIn('env-demo-b', text)

    def test_datasource_usage(self):
        text = self.call('yearning_datasource_usage')
        self.assertIn('demo_db', text)
        self.assertIn('75.0%', text)          # 30 / 40

    def test_board(self):
        self.assertIn('公告板', self.call('yearning_board'))

    def test_orders(self):
        text = self.call('yearning_orders', page=2, pagesize=5)
        self.assertIn('工单列表 (page=2, pagesize=5)', text)

    def test_orders_rejects_bad_pagesize(self):
        with self.assertRaises(ToolError):
            self.call('yearning_orders', pagesize=0)
        with self.assertRaises(ToolError):
            self.call('yearning_orders', pagesize=9999)

    def test_my_orders(self):
        text = self.call('yearning_my_orders')
        self.assertIn('20260101120000123', text)
        self.assertIn('DML', text)
        self.assertIn('auditor-a', text)

    def test_query_status(self):
        text = self.call('yearning_query_status')
        self.assertIn('已生效', text)
        self.assertIn('env-demo-a', text)

    def test_query_status_when_pending(self):
        self.fresh(query_state=2)
        text = self.call('yearning_query_status')
        self.assertIn('待审核', text)

    def test_read_tools_never_write(self):
        """跑一遍只读工具，确认除登录外没有任何非 GET 请求。"""
        for name in READ_TOOLS:
            self.call(name, idc='env-demo-a')
        # api_get 需要显式 path，没法跟着上面的循环一起传 idc，单独补一次 ——
        # 它是唯一能透传任意路径的工具，漏掉它这个用例就没守住最该守的地方。
        self.call('yearning_api_get', path='/api/v2/dash/count')
        self.assertEqual(self.fake.write_paths(), [],
                         '只读工具不该发出任何非 GET 请求')

    # ------------------------------------------------------------- 执行查询

    def test_run_query_happy_path(self):
        text = self.call('yearning_run_query', source='demo_db_src',
                         data_base='demo_db', sql='SELECT * FROM t')
        self.assertIn('查询已执行', text)
        self.assertIn('alpha', text)
        self.assertIn('beta', text)
        self.assertIn('/api/v2/query/results', self.fake.write_paths())

    def test_run_query_sends_expected_body(self):
        self.call('yearning_run_query', source='demo_db_src',
                  data_base='demo_db', sql='SELECT 1')
        bodies = self.fake.write_bodies('/api/v2/query/results')
        self.assertEqual(len(bodies), 1)
        self.assertEqual(sorted(bodies[0]), sorted(['source', 'data_base', 'sql']))
        self.assertEqual(bodies[0]['source'], 'demo_db_src')

    def test_run_query_resolves_whitespace_source(self):
        """用户给 strip 过的名字，发出去的必须是数据库里的原样字符串。"""
        self.call('yearning_run_query', source='db-readonly',
                  data_base='demo_db', sql='SELECT 1')
        bodies = self.fake.write_bodies('/api/v2/query/results')
        self.assertEqual(bodies[0]['source'], ' db-readonly')

    def test_run_query_rejects_write_sql_without_calling(self):
        for sql in ('DELETE FROM t', 'SELECT 1; DROP TABLE t',
                    'SELECT * FROM t INTO OUTFILE "/tmp/x"'):
            with self.subTest(sql=sql):
                with self.assertRaises(ToolError) as ctx:
                    self.call('yearning_run_query', source='demo_db_src',
                              data_base='demo_db', sql=sql)
                self.assertIn('拒绝', str(ctx.exception))
        self.assertEqual(self.fake.write_bodies('/api/v2/query/results'), [],
                         '危险 SQL 不能被发出去')

    def test_run_query_requires_active_query_order(self):
        self.fresh(query_state=2)
        with self.assertRaises(ToolError) as ctx:
            self.call('yearning_run_query', source='demo_db_src',
                      data_base='demo_db', sql='SELECT 1')
        self.assertIn('没有生效中的查询工单', str(ctx.exception))
        self.assertEqual(self.fake.write_bodies('/api/v2/query/results'), [])

    def test_run_query_rejects_source_outside_query_idc(self):
        """上游不校验数据源属于哪个环境 —— 这里补上。"""
        self.fresh(query_idc='env-demo-b')      # env-demo-b 没有任何查询权限
        with self.assertRaises(ToolError) as ctx:
            self.call('yearning_run_query', source='demo_db_src',
                      data_base='demo_db', sql='SELECT 1')
        self.assertIn('拒绝', str(ctx.exception))
        self.assertEqual(self.fake.write_bodies('/api/v2/query/results'), [],
                         '越环境的数据源不能被查')

    def test_run_query_requires_arguments(self):
        for missing in ({}, {'source': 's'}, {'source': 's', 'data_base': 'd'}):
            with self.subTest(missing=missing):
                args = dict(missing)
                args.setdefault('sql', 'SELECT 1')
                with self.assertRaises(ToolError):
                    self.call('yearning_run_query', **args)

    # ------------------------------------------------------------- 提交工单

    def test_submit_order_preview_does_not_submit(self):
        text = self.call('yearning_submit_order', idc='env-demo-a',
                         source='demo_db_src', data_base='demo_db',
                         sql='UPDATE t SET a=1;', type='dml', text='修数据',
                         assigned='auditor-a')
        self.assertIn('尚未提交', text)
        self.assertIn('confirm=true', text)
        self.assertIn('env-demo-a', text)
        self.assertEqual(self.fake.write_bodies('/api/v2/common/order'), [],
                         '预览阶段不能真的提工单')

    def test_submit_order_with_confirm(self):
        text = self.call('yearning_submit_order', idc='env-demo-a',
                         source='demo_db_src', data_base='demo_db',
                         sql='UPDATE t SET a=1;', type='dml', text='修数据',
                         assigned='auditor-a', confirm=True)
        self.assertIn('工单已提交', text)
        bodies = self.fake.write_bodies('/api/v2/common/order')
        self.assertEqual(len(bodies), 1)
        body = bodies[0]
        self.assertEqual(body['type'], 1)
        self.assertEqual(body['idc'], 'env-demo-a')
        self.assertEqual(body['source'], 'demo_db_src')
        self.assertEqual(body['assigned'], 'auditor-a')
        self.assertEqual(body['delay'], 'none')
        self.assertEqual(body['backup'], 1)

    def test_submit_order_accepts_string_confirm(self):
        """MCP 客户端有时把布尔传成字符串，真值表要宽容但保守。"""
        self.call('yearning_submit_order', idc='env-demo-a',
                  source='demo_db_src', data_base='demo_db',
                  sql='UPDATE t SET a=1;', type='dml', text='x',
                  assigned='auditor-a', confirm='true')
        self.assertEqual(len(self.fake.write_bodies('/api/v2/common/order')), 1)

    def test_submit_order_false_string_does_not_submit(self):
        self.call('yearning_submit_order', idc='env-demo-a',
                  source='demo_db_src', data_base='demo_db',
                  sql='UPDATE t SET a=1;', type='dml', text='x',
                  assigned='auditor-a', confirm='false')
        self.assertEqual(self.fake.write_bodies('/api/v2/common/order'), [])

    def test_submit_order_validates_locally_before_any_request(self):
        """参数/类型/SQL 类型错误必须在本地拦掉，连 fetch/source 都不该请求。

        顺序是有意的：本地校验 → 权限清单 → 审核人 → 提交。
        否则一个手滑的参数就会先在线上留一条查询痕迹。
        """
        for kwargs in (
            {'sql': 'SELECT * FROM t', 'type': 'dml'},        # SELECT 提 DML
            {'sql': 'ALTER TABLE t ADD c INT', 'type': 'dml'},  # 类型不匹配
            {'sql': 'UPDATE t SET a=1;', 'type': 'truncate'},   # 类型名非法
            {'sql': '   ', 'type': 'dml'},                      # SQL 为空
        ):
            with self.subTest(**kwargs):
                before = len(self.fake.requests)
                args = {'idc': 'env-demo-a', 'source': 'demo_db_src',
                        'data_base': 'demo_db', 'text': 'x',
                        'assigned': 'auditor-a'}
                args.update(kwargs)
                with self.assertRaises(ToolError):
                    self.call('yearning_submit_order', **args)
                new = [p for _m, p in self.fake.requests[before:]]
                self.assertEqual(new, [],
                                 '本地校验失败时不该发请求，实际发了：%s' % new)

    def test_submit_order_rejects_unauthorized_source(self):
        """服务端不校验权限 —— 闸门必须在这里挡下。"""
        with self.assertRaises(ToolError) as ctx:
            self.call('yearning_submit_order', idc='env-demo-b',
                      source='other_src', data_base='d', sql='UPDATE t SET a=1;',
                      type='dml', text='x', assigned='auditor-a', confirm=True)
        self.assertIn('拒绝', str(ctx.exception))
        self.assertEqual(self.fake.write_bodies('/api/v2/common/order'), [],
                         '未授权的数据源不能被提交')

    def test_submit_order_rejects_unknown_source(self):
        with self.assertRaises(ToolError):
            self.call('yearning_submit_order', idc='env-demo-a',
                      source='not_a_source', data_base='d',
                      sql='UPDATE t SET a=1;', type='dml', text='x',
                      assigned='auditor-a')
        self.assertEqual(self.fake.write_bodies('/api/v2/common/order'), [])

    def test_submit_order_rejects_select(self):
        """SELECT 不该被提成 DML 工单 —— 会被引导去查询通道。"""
        with self.assertRaises(ToolError) as ctx:
            self.call('yearning_submit_order', idc='env-demo-a',
                      source='demo_db_src', data_base='demo_db',
                      sql='SELECT * FROM t', type='dml', text='x',
                      assigned='auditor-a')
        self.assertIn('查询工单', str(ctx.exception))
        self.assertEqual(self.fake.write_bodies('/api/v2/common/order'), [])

    def test_submit_order_rejects_type_mismatch(self):
        with self.assertRaises(ToolError):
            self.call('yearning_submit_order', idc='env-demo-a',
                      source='demo_db_src', data_base='demo_db',
                      sql='ALTER TABLE t ADD c INT', type='dml', text='x',
                      assigned='auditor-a')

    def test_submit_order_rejects_bad_auditor(self):
        with self.assertRaises(ToolError) as ctx:
            self.call('yearning_submit_order', idc='env-demo-a',
                      source='demo_db_src', data_base='demo_db',
                      sql='UPDATE t SET a=1;', type='dml', text='x',
                      assigned='not_an_auditor')
        self.assertIn('审核人', str(ctx.exception))

    def test_submit_order_requires_auditor(self):
        with self.assertRaises(ToolError) as ctx:
            self.call('yearning_submit_order', idc='env-demo-a',
                      source='demo_db_src', data_base='demo_db',
                      sql='UPDATE t SET a=1;', type='dml', text='x')
        self.assertIn('审核人', str(ctx.exception))

    def test_submit_order_requires_text(self):
        with self.assertRaises(ToolError):
            self.call('yearning_submit_order', idc='env-demo-a',
                      source='demo_db_src', data_base='demo_db',
                      sql='UPDATE t SET a=1;', type='dml', text='  ',
                      assigned='auditor-a')

    def test_submit_order_rejects_bad_type_name(self):
        with self.assertRaises(ToolError):
            self.call('yearning_submit_order', idc='env-demo-a',
                      source='demo_db_src', data_base='demo_db',
                      sql='UPDATE t SET a=1;', type='truncate', text='x',
                      assigned='auditor-a')

    def test_submit_order_accepts_ddl(self):
        text = self.call('yearning_submit_order', idc='env-demo-a',
                         source='demo_db_src', data_base='demo_db',
                         sql='ALTER TABLE t ADD COLUMN c INT;', type='ddl',
                         text='加字段', assigned='auditor-a', confirm=True)
        self.assertIn('工单已提交', text)
        self.assertEqual(self.fake.write_bodies('/api/v2/common/order')[0]['type'], 0)

    # ------------------------------------------------------------- 提交查询工单

    def test_submit_query_order_preview_then_confirm(self):
        text = self.call('yearning_submit_query_order', idc='env-demo-a',
                         text='需要排查线上数据异常')
        self.assertIn('尚未提交', text)
        self.assertEqual(self.fake.write_bodies('/api/v2/query/refer'), [])

        text = self.call('yearning_submit_query_order', idc='env-demo-a',
                         text='需要排查线上数据异常', confirm=True)
        self.assertIn('查询工单已提交', text)
        self.assertEqual(len(self.fake.write_bodies('/api/v2/query/refer')), 1)

    def test_submit_query_order_requires_text(self):
        with self.assertRaises(ToolError):
            self.call('yearning_submit_query_order', idc='env-demo-a')

    # ------------------------------------------------------------- 撤销

    def test_revoke_order_requires_confirm(self):
        text = self.call('yearning_revoke', target='order',
                         work_id='20260101120000123')
        self.assertIn('尚未提交', text)
        self.assertNotIn('/api/v2/fetch/undo', self.fake.paths())

    def test_revoke_order(self):
        text = self.call('yearning_revoke', target='order',
                         work_id='20260101120000123', confirm=True)
        self.assertIn('工单已撤销', text)
        self.assertIn('/api/v2/fetch/undo', self.fake.paths())

    def test_revoke_order_requires_work_id(self):
        with self.assertRaises(ToolError):
            self.call('yearning_revoke', target='order', confirm=True)

    def test_revoke_query(self):
        text = self.call('yearning_revoke', target='query', confirm=True)
        self.assertIn('结束', text)
        self.assertIn('DELETE', repr(self.fake.writes[-1][0]))
        self.assertEqual(self.fake.writes[-1][1], '/api/v2/query')

    def test_revoke_rejects_bad_target(self):
        with self.assertRaises(ToolError):
            self.call('yearning_revoke', target='everyone', confirm=True)

    # ------------------------------------------------------------- 拦截

    def test_api_get_allows_readonly_path(self):
        text = self.call('yearning_api_get', path='/api/v2/dash/count')
        self.assertIn('HTTP 200', text)
        self.assertIn('createUser', text)

    def test_api_get_refuses_dangerous_paths_without_calling_them(self):
        refused = [
            '/api/v2/query/results',
            '/api/v2/query/refer',
            '/api/v2/fetch/undo',
            '/api/v2/fetch/merge',
            '/api/v2/fetch/roll_order',
            '/api/v2/audit/order/1',
            '/api/v2/manage/user',
            'http://evil.example/api/v2/dash/count',
        ]
        for path in refused:
            with self.subTest(path=path):
                with self.assertRaises(ToolError) as ctx:
                    self.call('yearning_api_get', path=path)
                self.assertIn('拒绝', str(ctx.exception))

        called = set(self.fake.paths())
        for path in ('/api/v2/query/results', '/api/v2/query/refer',
                     '/api/v2/fetch/undo', '/api/v2/fetch/merge',
                     '/api/v2/manage/user'):
            self.assertNotIn(path, called, '不该请求 %s' % path)

    def test_api_get_never_touches_perform(self):
        with self.assertRaises(ToolError):
            self.call('yearning_api_get', path='/api/v2/fetch/perform')
        self.assertEqual(self.fake.sensitive_hits, [],
                         '/api/v2/fetch/perform 绝不能被请求到')

    def test_audit_and_manage_actions_never_exposed(self):
        """把所有工具按各种参数过一遍，确认没有任何请求落到审批/管理域。"""
        self.call('yearning_status')
        self.call('yearning_my_orders')
        self.call('yearning_query_status')
        self.call('yearning_allowed_sources', idc='env-demo-a')
        for path in self.fake.paths():
            self.assertFalse(path.startswith('/api/v2/audit/'), path)
            self.assertFalse(path.startswith('/api/v2/manage/'), path)

    def test_illegal_response_becomes_clear_error(self):
        with self.assertRaises(ToolError) as ctx:
            self.call('yearning_api_get', path='/api/v2/does/not/exist')
        self.assertIn('Illegal', str(ctx.exception))

    def test_status_reports_unreachable_endpoint(self):
        dead = YearningClient({'endpoint': 'http://127.0.0.1:9',
                               'username': 'u', 'password': 'p'},
                              session_path=self.session_path, timeout=3)
        tools = dict((t['name'], t) for t in build_tools(dead, 'dead'))
        text = tools['yearning_status']['handler']({})
        self.assertIn('不可达', text)
        self.assertIn('VPN', text)


if __name__ == '__main__':
    unittest.main()
