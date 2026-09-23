# -*- coding: utf-8 -*-
"""写操作的业务闸门与纯逻辑。

分工：本模块**只做纯逻辑** —— SQL 分类、只读性判定、工单类型校验、
数据源比对、工单体构造。它不发任何请求，所以能完全离线做单元测试。
HTTP 编排在 :mod:`yearning2_mcp.tools` 里。

为什么需要这些闸门
------------------

上游 Yearning 2.x 有两个**服务端缺失的校验**，必须由本 MCP 补上：

1. ``SQLReferToOrder``（提 DDL/DML 工单）与 ``ReferQueryOrder``（提查询工单）
   里**一行权限校验都没有**。``ddl_source`` / ``dml_source`` 的过滤只发生在
   ``GET /api/v2/fetch/source`` —— 那是给前端下拉框用的。也就是说，
   只要知道数据源名字，任何人都能绕过下拉框对任意库提工单。
   → 补法：提交前先查 ``fetch/source``，比对通过才发，见 :func:`resolve_source`。

2. ``POST /api/v2/query/results`` 只看「有没有生效中的查询工单」，
   **不校验数据源属于哪个环境**。批准了 A 环境，照样能查 B 环境的库。
   → 补法：执行前用查询工单的 ``idc`` 反查可查清单，见 :func:`resolve_source`。

另外两类闸门在上面两条之外：

* **SQL 只读性**（:func:`ensure_read_only`）—— 查询通道只许跑只读语句。
  上游对 ``query/results`` 没有任何 SQL 类型限制，服务端把 SQL 转发给 SOAR
  服务做 LIMIT 改写，但不会拒绝写语句。
* **工单类型自洽**（:func:`check_order_type`）—— 防止把 SELECT 提成 DDL/DML
  工单，让审核人在 SOAR 审计里看到莫名其妙的报错。

只读判定的设计取舍
------------------

只读校验刻意做得**精确而非宽泛**：黑词表那种做法（把 ``SET``、``USE``、
``EVENT``、``TRIGGER`` 等全塞进去）会让 ``SELECT event FROM logs`` 这种正常
查询被误杀。这里改用三条互相独立的规则，见 :func:`ensure_read_only` 的文档。
"""
import re

# --- 语句分类 ---------------------------------------------------------------

# 只读语句的起始词。TABLE / VALUES 是 MySQL 的简写形式（TABLE t 等价 SELECT * FROM t）
READ_STARTERS = frozenset([
    'SELECT', 'WITH', 'SHOW', 'DESC', 'DESCRIBE', 'EXPLAIN', 'TABLE',
    'VALUES', 'HELP',
])

# DDL / DML 语句的起始词
DDL_STARTERS = frozenset(['CREATE', 'ALTER', 'DROP', 'TRUNCATE', 'RENAME'])
DML_STARTERS = frozenset(['INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'MERGE'])

# 第一种只读起始词：后面跟的仍是同一条语句，但**真正执行的动作在关键字之后**。
# MySQL 8 支持 ``WITH cte AS (...) UPDATE/DELETE/INSERT ...``；
# ``EXPLAIN ANALYZE <stmt>`` 会真的把 <stmt> 跑一遍。
COMPOUND_HEADS = frozenset(['WITH', 'EXPLAIN'])

# 这些语句里不允许出现的写动词（配合 COMPOUND_HEADS 使用）
WRITE_VERBS = frozenset(['INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'MERGE',
                         'CREATE', 'ALTER', 'DROP', 'TRUNCATE', 'RENAME',
                         'GRANT', 'REVOKE'])

# 任何只读语句里都不允许出现的文件读写关键字
FILE_WORDS = frozenset(['OUTFILE', 'DUMPFILE', 'LOAD_FILE'])

# 会占用/阻塞数据库连接的函数，只读通道一律拒绝（防自己把实例拖慢）
DANGEROUS_FUNCTIONS = frozenset(['SLEEP', 'BENCHMARK', 'GET_LOCK',
                                 'RELEASE_LOCK', 'MASTER_POS_WAIT'])

# 锁语义的二元词组
LOCKING_BIGRAMS = (('FOR', 'UPDATE'), ('LOCK', 'IN'), ('FOR', 'SHARE'))

# 一条 SQL 里允许的语句条数上限（防止有人塞一整个 dump 进来）
MAX_STATEMENTS = 50


def strip_sql_noise(sql):
    """去掉注释、字符串字面量、反引号标识符，只留下结构。

    单趟状态机而不是正则替换 —— ``--`` 与 ``#`` 出现在字符串里时不是注释，
    正则处理必然出错（例如 ``SELECT '--'``）。

    字符串字面量替换成 ``''``、反引号标识符替换成 ``X``，
    这样 ``SELECT 'drop table t'`` 不会被误判成写语句。
    """
    text = str(sql or '')
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ''

        if ch == '/' and nxt == '*':                      # 块注释（含 /*! hint */）
            end = text.find('*/', i + 2)
            i = n if end < 0 else end + 2
            out.append(' ')
            continue

        if (ch == '-' and nxt == '-') or ch == '#':        # 行注释
            end = text.find('\n', i)
            i = n if end < 0 else end + 1
            out.append(' ')
            continue

        if ch in ("'", '"', '`'):
            quote = ch
            i += 1
            while i < n:
                if text[i] == '\\' and quote != '`':       # 反斜杠转义（反引号不适用）
                    i += 2
                    continue
                if text[i] == quote:
                    if i + 1 < n and text[i + 1] == quote:  # '' 与 `` 的重复转义
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append('X' if quote == '`' else "''")
            continue

        out.append(ch)
        i += 1
    return ''.join(out)


def split_statements(sql):
    """按 ``;`` 拆成语句，去掉空语句，返回去噪后的语句列表。"""
    clean = strip_sql_noise(sql)
    return [chunk.strip() for chunk in clean.split(';') if chunk.strip()]


def words_of(statement):
    """取语句里的词序列（大写）。"""
    return re.findall(r'[A-Za-z_][A-Za-z_0-9]*', str(statement).upper())


def has_word(statement, word):
    """整词匹配。

    用显式前后瞻而不是 ``\\b`` —— 中文是 Python 的 word 字符，
    ``\\b`` 在中文相邻处行为不符合直觉。
    """
    return re.search(r'(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])' % word,
                     str(statement).upper()) is not None


def has_bigram(statement, first, second):
    """整词二元组匹配，用于 ``FOR UPDATE`` / ``LOCK IN`` 这类锁语义。"""
    return re.search(
        r'(?<![A-Za-z0-9_])%s\s+%s(?![A-Za-z0-9_])' % (first, second),
        str(statement).upper()) is not None


def classify(statement):
    """给单条语句分类：``read`` / ``ddl`` / ``dml`` / ``other``。"""
    words = words_of(statement)
    if not words:
        return 'other'
    head = words[0]
    if head in READ_STARTERS:
        return 'read'
    if head in DDL_STARTERS:
        return 'ddl'
    if head in DML_STARTERS:
        return 'dml'
    return 'other'


def ensure_read_only(sql):
    """校验 SQL 全部由只读语句构成。

    三条规则，互相独立：

    1. **每条语句的首词必须是只读起始词**。这一条就挡掉了 ``SET`` / ``USE`` /
       ``CALL`` / ``DROP`` 之类「只能作为首词生效」的动作 —— 所以不需要把
       它们塞进黑词表（那样会误伤 ``SELECT event FROM logs``）。
    2. **首词是 ``WITH`` 或 ``EXPLAIN`` 时，语句里不得出现写动词**，因为
       这两种前缀后面的语句才是真正执行的动作（``WITH ... DELETE``、
       ``EXPLAIN ANALYZE ...``）。
    3. **任何语句都不得出现 ``OUTFILE`` / ``DUMPFILE`` / ``LOAD_FILE``**，
       以及 ``SLEEP`` / ``BENCHMARK`` / ``GET_LOCK`` 这类会拖住连接的函数，
       和 ``FOR UPDATE`` / ``LOCK IN SHARE MODE`` 这类锁语义。

    通过返回 ``None``；否则返回**拒绝原因**（中文）。
    """
    raw = str(sql or '').strip()
    if not raw:
        return '拒绝: SQL 为空'

    statements = split_statements(raw)
    if not statements:
        return '拒绝: SQL 里没有有效语句'
    if len(statements) > MAX_STATEMENTS:
        return ('拒绝: 一次最多 %d 条语句，收到 %d 条。'
                '大批量脚本请在 Yearning 网页上走工单流程。'
                % (MAX_STATEMENTS, len(statements)))

    for idx, statement in enumerate(statements, 1):
        kind = classify(statement)
        if kind != 'read':
            return ('拒绝: 第 %d 条语句不是只读语句（判定为 %s）。\n'
                    '语句: %s\n'
                    '查询通道只执行 SELECT / SHOW / DESC / EXPLAIN 这类只读语句；'
                    '写操作请用 yearning_submit_order 提 DDL/DML 工单。'
                    % (idx, {'ddl': 'DDL', 'dml': 'DML'}.get(kind, '无法判定'),
                       clip(statement)))

        words = words_of(statement)
        head = words[0] if words else ''

        if head in COMPOUND_HEADS:
            for verb in sorted(WRITE_VERBS):
                if has_word(statement, verb):
                    return ('拒绝: 第 %d 条语句以 %s 开头，但语句里出现了 %s —— '
                            '前缀后面的语句才是真正执行的动作，这类写法会被拒绝。\n'
                            '语句: %s'
                            % (idx, head, verb, clip(statement)))

        for word in sorted(FILE_WORDS):
            if has_word(statement, word):
                return ('拒绝: 第 %d 条语句里出现了 %s。\n'
                        '语句: %s\n'
                        'SELECT ... INTO OUTFILE / DUMPFILE 可以往数据库服务器写文件，'
                        'LOAD_FILE 可以读服务器文件，都按写操作处理。'
                        % (idx, word, clip(statement)))

        for func in sorted(DANGEROUS_FUNCTIONS):
            if has_word(statement, func):
                return ('拒绝: 第 %d 条语句调用了 %s()。\n'
                        '语句: %s\n'
                        '这类函数会长时间占住数据库连接或操纵锁，'
                        '在共享的审核平台上不跑比较稳妥。'
                        % (idx, func, clip(statement)))

        for first, second in LOCKING_BIGRAMS:
            if has_bigram(statement, first, second):
                return ('拒绝: 第 %d 条语句带 %s %s 锁语义。\n'
                        '语句: %s\n'
                        '只读通道不加行锁 —— 会影响到生产库上的其他会话。'
                        % (idx, first, second, clip(statement)))
    return None


def check_order_type(sql, type_code):
    """校验 SQL 与工单类型是否自洽。

    ``type_code``：``0`` = DDL，``1`` = DML（Yearning 的约定）。

    通过返回 ``None``；否则返回**拒绝原因**（中文）。
    """
    raw = str(sql or '').strip()
    if not raw:
        return '拒绝: SQL 为空'

    statements = split_statements(raw)
    if not statements:
        return '拒绝: SQL 里没有有效语句'

    kinds = [classify(s) for s in statements]

    for idx, (statement, kind) in enumerate(zip(statements, kinds), 1):
        if kind == 'read':
            return ('拒绝: 第 %d 条是只读语句，不能提成 DDL/DML 工单。\n'
                    '语句: %s\n'
                    'SELECT 这类查询请用 yearning_submit_query_order 提交查询工单，'
                    '批准后由 yearning_run_query 执行。'
                    % (idx, clip(statement)))
        if kind == 'other':
            return ('拒绝: 第 %d 条语句无法判定类型（既不是 DDL 也不是 DML）。\n'
                    '语句: %s\n'
                    '为避免把无法审计的语句送进审核流程，这里一概拒绝；'
                    '确需提交请在 Yearning 网页上操作。'
                    % (idx, clip(statement)))

    has_dml = 'dml' in kinds
    has_ddl = 'ddl' in kinds

    if has_dml and type_code != 1:
        return ('拒绝: SQL 里含 DML 语句，工单类型必须是 DML（type=1），'
                '当前给的是 %s。混有 DDL 与 DML 时按 DML 提交更安全。'
                % _type_name(type_code))
    if has_ddl and not has_dml and type_code != 0:
        return ('拒绝: SQL 全是 DDL 语句，工单类型必须是 DDL（type=0），'
                '当前给的是 %s。' % _type_name(type_code))
    return None


def _type_name(type_code):
    return {0: 'DDL(type=0)', 1: 'DML(type=1)'}.get(type_code,
                                                    '未知类型(%r)' % (type_code,))


def clip(text, limit=160):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit] + ' ...'


# --- 数据源比对 -------------------------------------------------------------

def resolve_source(allowed, given):
    """在允许清单里解析出数据源名。

    返回**清单里的原始字符串**（可能带前后空格），解析不到返回 ``None``。

    为什么不返回用户传来的那个值：线上实例的数据源名里真的存在
    ``" db-2"`` 这种**带前导空格**的记录，而 Yearning 执行时是
    ``WHERE source = ?`` 精确匹配。所以比对要宽松（strip 后比），
    但发给服务端的值必须是数据库里的原样字符串，否则后续查不到。
    """
    want = str(given or '').strip()
    if not want:
        return None
    for item in allowed or ():
        if str(item) == want or str(item).strip() == want:
            return item
    return None


def missing_source_hint(allowed, idc, kind):
    """数据源不在允许清单里时的提示语。"""
    lines = ['拒绝: 数据源不在 %s 环境下你被授权的%s清单里。' % (idc, kind)]
    lines.append('')
    lines.append('这是 MCP 侧补上的闸门 —— Yearning 服务端对提工单不做权限校验，')
    lines.append('权限过滤只在前端下拉框里，所以这里必须自己挡。')
    if allowed:
        lines.append('')
        lines.append('你在该环境下可用的数据源：')
        for item in allowed:
            lines.append('  - %s' % str(item).strip())
    else:
        lines.append('')
        lines.append('该环境下你没有任何可用的数据源，需要管理员授权。')
    return '\n'.join(lines)


# --- 工单体构造 -------------------------------------------------------------

def order_payload(idc, source, data_base, sql, type_code, text,
                  table='', backup=1, delay='none', assigned=''):
    """构造 ``model.CoreSqlOrder`` 的请求体。

    字段名与上游结构体的 json tag 一一对应，不要凭感觉改：

    * ``type``  0=DDL 1=DML
    * ``backup`` 0/1，DML 是否先备份
    * ``delay``  ``"none"`` 或 ``"2006-01-02 15:04"`` 格式的定时执行时间
    * ``assigned`` 审核人，服务端会写进 ``relevant``，必须来自
      ``fetch/source`` 返回的 ``assigned`` 清单
    """
    return {
        'idc': idc,
        'source': source,
        'data_base': data_base,
        'table': table or '',
        'sql': sql,
        'text': text or '',
        'type': type_code,
        'backup': backup,
        'delay': delay or 'none',
        'assigned': assigned,
    }


def query_order_payload(idc, text, export=0, assigned=''):
    """构造查询工单请求体（``commom.QueryOrder``）。

    ``export`` 非 0 表示允许导出结果。
    """
    return {
        'idc': idc,
        'text': text or '',
        'export': int(export or 0),
        'assigned': assigned or '',
    }


def query_body(source, data_base, sql):
    """构造执行查询的请求体（``lib.QueryDeal``）。"""
    return {'source': source, 'data_base': data_base, 'sql': sql}
