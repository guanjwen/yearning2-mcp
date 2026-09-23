# -*- coding: utf-8 -*-
"""接口安全策略。

本 MCP 的能力分三层，这个模块是执行层：

1. **只读 READ** —— ``yearning_api_get`` 可透传的 GET。
   非 GET、写操作型 GET（如 ``fetch/undo``）一律拒绝。
2. **提交 WRITE** —— 提工单、执行只读查询、撤销自己的工单、结束查询权限。
   只能经**专用工具**调用：不接受任意路径，每个动作都要过
   :mod:`yearning2_mcp.writes` 里的业务闸门（权限清单、SQL 类型、两步确认）。
3. **拦截 BLOCKED** —— 审批类、管理类、泄露密码哈希的接口，
   以及会绕过审核直接改数据的执行类接口。

设计取舍说明
------------

上游 Yearning 2.x 的 ``SQLReferToOrder``（提 DDL/DML 工单）与
``ReferQueryOrder``（提查询工单）在**服务端一行权限校验都没有** ——
``ddl_source`` / ``dml_source`` 的过滤只发生在 ``fetch/source`` 接口里，
也就是前端下拉框。所以本 MCP 必须自己把闸门补上，
见 :func:`yearning2_mcp.writes.gate_source`。

没有 ``--force`` 之类的后门。要放开只能改这个模块 —— 属刻意行为，
改完请同步改 README 与 SECURITY.md。
"""
from urllib.parse import unquote

API_PREFIX = '/api/v2/'

# --- 1. 拦截：会绕过审核直接改数据的执行类 ---------------------------------
BLOCKED_EXACT = {
    '/api/v2/fetch/test': '会让审核服务真的执行 SQL 测试',
    '/api/v2/fetch/merge': 'SOAR 合并接口，尚未开放（如需分析 DDL 合并请提工单）',
    '/api/v2/fetch/marge': '超级用户规则合并（写操作）',
    '/api/v2/fetch/roll_order': '会创建回滚工单，尚未开放',
}

# --- 2. 拦截：整个写操作域 -------------------------------------------------
BLOCKED_PREFIX = {
    '/api/v2/audit/': '审批类接口会改变工单状态',
    '/api/v2/manage/board': '公告写操作',
    '/api/v2/manage/db': '数据源配置写操作',
    '/api/v2/manage/roles': '角色配置写操作',
    '/api/v2/manage/setting': '系统设置写操作',
    '/api/v2/manage/task': '自动任务写操作',
    '/api/v2/manage/tpl': '模板写操作',
    '/api/v2/manage/user': '用户管理写操作',
}

# --- 3. 拦截：只读但含敏感字段 ---------------------------------------------
BLOCKED_SENSITIVE = {
    '/api/v2/fetch/perform':
        '返回内容包含执行人的 PBKDF2 密码哈希，不允许经 MCP 带进对话记录',
}

# 前缀黑名单的只读例外（先判例外，再判前缀）
ALLOWED_EXCEPTIONS = (
    '/api/v2/manage/group',
)

# --- 4. 写操作型 GET：路径看着像只读，实际会改数据 --------------------------
GET_BLOCKED_EXACT = {
    '/api/v2/fetch/undo':
        '撤销工单是写操作（会删除工单记录），请用工具 yearning_revoke',
    '/api/v2/query/results':
        '执行 SQL 是 POST 动作，请用工具 yearning_run_query',
    '/api/v2/query/refer':
        '提交查询工单是 POST 动作，请用工具 yearning_submit_query_order',
}

# --- 5. 允许的写动作白名单 -------------------------------------------------
# 专用工具只能按这张表发请求，路径与方法是写死的，调用方无法自定义。
# 每个值：(HTTP 方法, 路径, 说明)
WRITE_ACTIONS = {
    'submit_order':       ('POST', '/api/v2/common/order',
                           '提交 DDL / DML 工单'),
    'submit_query_order': ('POST', '/api/v2/query/refer',
                           '提交查询工单（SELECT 的入口）'),
    'run_query':          ('POST', '/api/v2/query/results',
                           '执行只读查询（需查询工单已生效）'),
    'my_orders':          ('PUT', '/api/v2/common/list',
                           '读取我提交的工单'),
    'query_status':       ('PUT', '/api/v2/query/status',
                           '读取查询工单状态'),
    'cancel_order':       ('GET', '/api/v2/fetch/undo',
                           '撤销我提交的待审工单'),
    'end_query':          ('DELETE', '/api/v2/query',
                           '结束我自己的查询权限'),
}

# 需要两步确认（先预览、显式 confirm 才真执行）的动作。
#
# 注意 run_query 不在里面：它是**只读且可重复执行**的，每次都要求确认会让
# 探索式查询没法用。它的保护来自另外两道闸门 —— SQL 只读性校验 +
# 数据源必须属于已生效查询工单的环境（见 writes.resolve_source）。
CONFIRM_REQUIRED = ('submit_order', 'submit_query_order',
                    'cancel_order', 'end_query')


def normalize_path(path):
    """归一化：剥掉查询串/锚点、百分号解码、去尾部斜杠、转小写。

    归一化是为了让 ``/api/v2/query/results/`` 、``%2e%2e`` 这类变体
    也命中同一套规则，避免绕过后门。
    """
    p = str(path or '').strip()
    p = p.split('#', 1)[0].split('?', 1)[0]
    try:
        p = unquote(p)
    except Exception:
        pass
    p = p.rstrip('/')
    return p.lower()


def check_get_path(path):
    """检查一个只读 GET 路径是否放行。

    放行返回 ``None``；否则返回**拒绝原因**（中文，可直接回给调用方）。
    """
    raw = str(path or '').strip()
    if not raw:
        return '拒绝: 缺少 path 参数'

    # 所有判定都在归一化后的路径上做 —— 否则 %2e%2e 这类编码形式能绕过。
    norm = normalize_path(raw)
    if '..' in norm:
        return '拒绝: path 含 ".."（含百分号编码形式）'
    if '\\' in norm:
        return '拒绝: path 不能含反斜杠'
    if '://' in norm:
        return '拒绝: path 不能是完整 URL，只能是 /api/v2/ 下的相对路径'
    if not norm.startswith(API_PREFIX):
        return '拒绝: path 必须以 %s 开头（本 MCP 只访问该前缀下的接口）' % API_PREFIX

    if norm in GET_BLOCKED_EXACT:
        return '拒绝: %s\n%s' % (raw, GET_BLOCKED_EXACT[norm])

    lowered_exceptions = tuple(x.lower() for x in ALLOWED_EXCEPTIONS)
    for allow in lowered_exceptions:
        if norm == allow or norm.startswith(allow + '/'):
            return None

    if norm in BLOCKED_SENSITIVE:
        return '拒绝: %s\n%s' % (raw, BLOCKED_SENSITIVE[norm])

    if norm in BLOCKED_EXACT:
        return ('拒绝: %s 属于高危接口（%s），MCP 层已硬拦截。'
                % (raw, BLOCKED_EXACT[norm]))

    for prefix, why in BLOCKED_PREFIX.items():
        if norm.startswith(prefix):
            return '拒绝: %s 属于写操作域（%s），MCP 层已硬拦截。' % (raw, why)

    return None


def check_write(action):
    """检查一个写动作是否在允许清单里。

    返回 ``(method, path)``；未知动作抛 :class:`ValueError`。
    """
    if action not in WRITE_ACTIONS:
        raise ValueError('未知写动作: %s（允许: %s）'
                         % (action, ', '.join(sorted(WRITE_ACTIONS))))
    method, path, _desc = WRITE_ACTIONS[action]
    return method, path


def describe_action(action):
    """写动作的中文说明。"""
    return WRITE_ACTIONS[action][2]


def needs_confirm(action):
    """该动作是否需要两步确认。"""
    return action in CONFIRM_REQUIRED


def blocked_summary():
    """给文档/自检用的规则摘要。"""
    return {
        'exact': dict(BLOCKED_EXACT),
        'prefix': dict(BLOCKED_PREFIX),
        'sensitive': dict(BLOCKED_SENSITIVE),
        'get_exact': dict(GET_BLOCKED_EXACT),
        'allowed_exceptions': list(ALLOWED_EXCEPTIONS),
    }


def write_summary():
    """写动作摘要，供文档与自检使用。"""
    return dict((k, '%s %s —— %s' % (v[0], v[1], v[2]))
                for k, v in WRITE_ACTIONS.items())


def blocked_hint():
    """拒绝时附在末尾的规则说明。"""
    summary = blocked_summary()
    return ('\n\n本 MCP 的能力边界：\n'
            '  ✅ 只读: 统计、环境、数据源、公告板、工单明细等 GET 接口\n'
            '  ✅ 提交: 提 DDL/DML 工单、提查询工单、执行只读查询、撤销自己的工单\n'
            '  ⛔ 一律拒绝 —— 高危接口: %s\n'
            '                   写操作域: %s\n'
            '                   敏感接口: %s\n'
            '                   写操作型 GET: %s\n'
            '  放行例外: %s'
            % ('、'.join(sorted(summary['exact'])),
               '、'.join(sorted(summary['prefix'])),
               '、'.join(sorted(summary['sensitive'])),
               '、'.join(sorted(summary['get_exact'])),
               '、'.join(summary['allowed_exceptions'])))
