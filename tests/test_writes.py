# -*- coding: utf-8 -*-
"""写操作闸门的用例 —— 表驱动，纯离线。

这些用例的价值在于**双向覆盖**：既验证危险语句被拒，
也验证正常语句不被误杀。黑词表那种做法把所有
``SET`` / ``USE`` / ``EVENT`` 塞进去，会让 ``SELECT event FROM logs``
被误杀 —— 下面 ``ALLOWED`` 里特意留了这类反例。
"""
import unittest

from yearning2_mcp import writes


class StripNoiseTest(unittest.TestCase):

    def test_line_comment_removed(self):
        clean = writes.strip_sql_noise('SELECT 1 -- drop table t\nFROM x')
        self.assertNotIn('--', clean)
        self.assertNotIn('drop', clean.lower())
        self.assertIn('SELECT', clean)

    def test_block_comment_removed(self):
        clean = writes.strip_sql_noise('/* drop table t */ SELECT 1')
        self.assertNotIn('drop', clean.lower())
        self.assertIn('SELECT', clean)

    def test_comment_marker_inside_string_is_not_a_comment(self):
        """``SELECT '--'`` 里的 ``--`` 不是注释 —— 正则处理这里必然出错。"""
        clean = writes.strip_sql_noise("SELECT '--' AS x FROM t")
        self.assertIn('FROM', clean)
        self.assertIn('SELECT', clean)

    def test_hash_inside_string_is_not_a_comment(self):
        clean = writes.strip_sql_noise("SELECT '#not a comment' FROM t")
        self.assertIn('FROM', clean)

    def test_string_literal_neutralised(self):
        clean = writes.strip_sql_noise("SELECT 'DROP TABLE t'")
        self.assertNotIn('DROP', clean.upper())
        self.assertIn("''", clean)

    def test_backtick_identifier_neutralised(self):
        clean = writes.strip_sql_noise('SELECT `drop` FROM t')
        self.assertNotIn('drop', clean.lower())
        self.assertIn('X', clean)

    def test_escaped_quote(self):
        clean = writes.strip_sql_noise(r"SELECT 'a\'b' FROM t")
        self.assertIn('FROM', clean)


class EnsureReadOnlyTest(unittest.TestCase):

    ALLOWED = [
        'SELECT * FROM t',
        'SELECT 1',
        "SELECT 'drop table t'",                       # 字符串里的写词不算
        'SELECT `drop` FROM t',                        # 反引号标识符不算
        'SELECT 1; -- 尾注释',
        '/* 前置注释 */ SELECT 1',
        'SELECT * FROM t WHERE note = "delete from x"',
        'SHOW TABLES',
        'SHOW CREATE TABLE t',                         # 纯元数据读取
        'SHOW GRANTS FOR u',
        'SHOW FULL FIELDS FROM t',
        'DESC t',
        'DESCRIBE t',
        'EXPLAIN SELECT * FROM t',
        'TABLE t',
        'VALUES ROW(1)',
        'SELECT * FROM a; SELECT * FROM b',            # 多条只读
        # 下面这条是回归用例：早期的大黑词表会把 event / set_at / load_user 全炸掉
        'SELECT event, set_at, start_time, load_user, domain, use_flag FROM logs',
        'SELECT COUNT(*) FROM t WHERE name = "update"',
    ]

    DENIED = [
        '',
        '   ',
        ';',
        'INSERT INTO t VALUES (1)',
        'UPDATE t SET a=1',
        'DELETE FROM t',
        'REPLACE INTO t VALUES (1)',
        'DROP TABLE t',
        'ALTER TABLE t ADD COLUMN c INT',
        'TRUNCATE TABLE t',
        'CREATE TABLE t (id INT)',
        'RENAME TABLE a TO b',
        'SET @a = 1',
        'SET NAMES utf8mb4',
        'USE mysql',
        'CALL some_proc()',
        'LOCK TABLES t WRITE',
        'GRANT SELECT ON *.* TO u',
        'SELECT 1; DROP TABLE t',                      # 第二条是写
        '/* c */ DELETE FROM t',
        'WITH c AS (SELECT 1) DELETE FROM t',           # WITH 后面才是真动作
        'EXPLAIN ANALYZE DELETE FROM t',                # EXPLAIN ANALYZE 会真跑
        'SELECT * FROM t INTO OUTFILE "/tmp/x"',
        'SELECT * FROM t INTO DUMPFILE "/tmp/x"',
        'SELECT LOAD_FILE("/etc/passwd")',
        'SELECT SLEEP(30)',
        'SELECT BENCHMARK(10000000, MD5("x"))',
        'SELECT GET_LOCK("k", 10)',
        'SELECT * FROM t FOR UPDATE',
        'SELECT * FROM t LOCK IN SHARE MODE',
    ]

    def test_allowed(self):
        for sql in self.ALLOWED:
            with self.subTest(sql=sql):
                self.assertIsNone(writes.ensure_read_only(sql),
                                  '应当放行: %r' % sql)

    def test_denied(self):
        for sql in self.DENIED:
            with self.subTest(sql=sql):
                reason = writes.ensure_read_only(sql)
                self.assertIsNotNone(reason, '应当拒绝: %r' % sql)
                self.assertTrue(str(reason).startswith('拒绝'),
                                '拒绝原因应以「拒绝」开头: %r' % reason)

    def test_cte_select_is_allowed(self):
        """``WITH ... SELECT`` 是合法只读，不能被 WITH 规则误伤。"""
        self.assertIsNone(writes.ensure_read_only('WITH c AS (SELECT 1) SELECT * FROM c'))

    def test_statement_count_limit(self):
        many = '; '.join(['SELECT 1'] * (writes.MAX_STATEMENTS + 1))
        reason = writes.ensure_read_only(many)
        self.assertIsNotNone(reason)
        self.assertIn('最多', reason)

    def test_reason_points_at_offending_statement(self):
        reason = writes.ensure_read_only('SELECT 1;\nDROP TABLE t')
        self.assertIn('第 2 条', reason)


class OrderTypeTest(unittest.TestCase):

    def test_ddl_matches_ddl(self):
        self.assertIsNone(writes.check_order_type('ALTER TABLE t ADD c INT', 0))

    def test_ddl_rejected_as_dml(self):
        self.assertIsNotNone(writes.check_order_type('ALTER TABLE t ADD c INT', 1))

    def test_dml_matches_dml(self):
        self.assertIsNone(writes.check_order_type('UPDATE t SET a=1', 1))

    def test_dml_rejected_as_ddl(self):
        self.assertIsNotNone(writes.check_order_type('UPDATE t SET a=1', 0))

    def test_select_rejected_as_order(self):
        """SELECT 不该被提成 DDL/DML 工单 —— 会被引导去查询工单。"""
        for code in (0, 1):
            reason = writes.check_order_type('SELECT * FROM t', code)
            self.assertIsNotNone(reason)
            self.assertIn('查询工单', reason)

    def test_unclassifiable_rejected(self):
        reason = writes.check_order_type('SET NAMES utf8mb4', 0)
        self.assertIsNotNone(reason)
        self.assertIn('无法判定', reason)

    def test_mixed_prefers_dml(self):
        sql = 'ALTER TABLE a ADD c INT; INSERT INTO a VALUES (1)'
        self.assertIsNone(writes.check_order_type(sql, 1))
        self.assertIsNotNone(writes.check_order_type(sql, 0))

    def test_string_literal_does_not_fake_ddl(self):
        """注释或字符串里的 DDL 不该让一条 SELECT 通过校验。"""
        reason = writes.check_order_type("SELECT '-- ALTER TABLE t' FROM t", 0)
        self.assertIsNotNone(reason)


class ResolveSourceTest(unittest.TestCase):

    def test_exact_hit(self):
        self.assertEqual(writes.resolve_source(['a', 'b'], 'a'), 'a')

    def test_returns_original_string_with_whitespace(self):
        """线上真有 " demo_readonly" 这种带前导空格的记录。

        比对要宽松（strip 后比），但返回值必须是**原样字符串** ——
        Yearning 执行时是 ``WHERE source = ?`` 精确匹配。
        """
        allowed = [' demo_readonly', 'demo_db_src']
        self.assertEqual(writes.resolve_source(allowed, 'demo_readonly'),
                         ' demo_readonly')
        self.assertEqual(writes.resolve_source(allowed, '  demo_readonly '),
                         ' demo_readonly')

    def test_miss_returns_none(self):
        self.assertIsNone(writes.resolve_source(['a'], 'z'))
        self.assertIsNone(writes.resolve_source([], 'a'))
        self.assertIsNone(writes.resolve_source(['a'], ''))
        self.assertIsNone(writes.resolve_source(['a'], None))
        self.assertIsNone(writes.resolve_source(None, 'a'))

    def test_case_sensitive(self):
        """数据源名大小写敏感 —— Yearning 是精确匹配。"""
        self.assertIsNone(writes.resolve_source(['Demo'], 'demo'))

    def test_hint_lists_available_sources(self):
        hint = writes.missing_source_hint(['a', ' b'], 'env-x', 'DDL')
        self.assertIn('拒绝', hint)
        self.assertIn('env-x', hint)
        self.assertIn('a', hint)
        self.assertIn('b', hint)

    def test_hint_when_empty(self):
        hint = writes.missing_source_hint([], 'env-x', '查询')
        self.assertIn('管理员', hint)


class PayloadTest(unittest.TestCase):

    def test_order_payload_field_names(self):
        """字段名必须与上游 model.CoreSqlOrder 的 json tag 一致。"""
        body = writes.order_payload('i', 's', 'd', 'SQL', 1, 'txt',
                                    table='t', backup=1, delay='none',
                                    assigned='aud')
        self.assertEqual(sorted(body), sorted([
            'idc', 'source', 'data_base', 'table', 'sql', 'text',
            'type', 'backup', 'delay', 'assigned']))
        self.assertEqual(body['type'], 1)
        self.assertEqual(body['delay'], 'none')

    def test_order_payload_defaults(self):
        body = writes.order_payload('i', 's', 'd', 'SQL', 0, 'txt')
        self.assertEqual(body['table'], '')
        self.assertEqual(body['backup'], 1)
        self.assertEqual(body['delay'], 'none')

    def test_query_body_field_names(self):
        self.assertEqual(sorted(writes.query_body('s', 'd', 'SQL')),
                         sorted(['source', 'data_base', 'sql']))

    def test_query_order_payload_field_names(self):
        body = writes.query_order_payload('i', 'txt', export=1)
        self.assertEqual(sorted(body), sorted(['idc', 'text', 'export', 'assigned']))
        self.assertEqual(body['export'], 1)


if __name__ == '__main__':
    unittest.main()
