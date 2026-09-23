# -*- coding: utf-8 -*-
"""MCP 工具定义与实现。

能力分三层（详见 :mod:`yearning2_mcp.safety`）：

* **只读** —— 统计、环境、数据源、公告板、工单明细。唯一的路径透传工具
  ``yearning_api_get`` 必须过 :func:`yearning2_mcp.safety.check_get_path`，且只能发 GET。
* **提交** —— 提 DDL/DML 工单、提查询工单、执行只读查询、撤销自己的工单。
  这些一律**走专用工具**，路径与方法写死在安全模块的白名单里，并且都要过
  :mod:`yearning2_mcp.writes` 的业务闸门。
* **拦截** —— 审批类、管理类、泄露密码哈希的接口，以及会绕过审核直接改数据的
  执行类接口，在 :mod:`yearning2_mcp.safety` 里被硬拒。
"""
import json
import unicodedata
from urllib.parse import quote

from . import safety, writes
from .client import ConnectionFailed, LoginFailed, YearningError

ILLEGAL_MARKER = '"Illegal"'

# 查询结果最多渲染多少行 —— 防止一条 SELECT 把对话上下文冲爆
MAX_RENDER_ROWS = 50

SOURCE_TYPES = ('ddl', 'dml', 'query', 'all')


class ToolError(Exception):
    """工具执行失败 —— 服务端会转成 isError=true 的返回。"""


def _jload(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _pretty(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def _payload_of(data):
    """Yearning 的响应统一是 ``{code, payload, text}``，这里剥出 payload。"""
    if isinstance(data, dict) and 'payload' in data:
        return data.get('payload')
    return data


def _qs(value):
    return quote(str(value))


def _width(text):
    """显示宽度：CJK 与全角标点占 2 列。

    ``'%-9s'`` 对中文是按**字符数**补空格，所以 ``执行前备份`` 比 ``环境``
    宽出一倍，输出会参差不齐 —— 中文标签的对齐必须自己按显示宽度算。
    """
    total = 0
    for ch in str(text):
        total += 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
    return total


def _pad(text, target):
    """把 text 补到 target 显示宽度（不含分隔符）。"""
    text = str(text)
    return text + ' ' * max(0, target - _width(text))


def _clip(text, limit=300):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit] + ' ...'


def _truthy(value):
    """宽容地解析 confirm 参数。

    ``True`` / ``"true"`` / ``"1"`` / ``"yes"`` / ``1`` 都算确认，
    ``None`` / ``False`` / ``"0"`` / ``"no"`` / ``""`` 都算未确认。
    故意不接受 ``"false"`` 之外的任意字符串 —— 保守优先。
    """
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ('true', '1', 'yes', 'y', 'on')


def build_tools(client, config_source):
    """为指定客户端构造工具列表。

    每个元素形如 ``{'name', 'description', 'inputSchema', 'handler'}``，
    ``handler(args) -> str``。
    """

    # ------------------------------------------------------------------ 底层

    def do(method, path, body=None):
        """发一个请求，返回 ``(status, data)``；``data`` 已解析成对象。"""
        try:
            client.ensure_login()
        except (ConnectionFailed, LoginFailed) as e:
            raise ToolError(str(e))
        status, text = client.request(method, path, body=body)
        if status is None:
            raise ToolError('请求失败（连不上或超时）：%s' % text)
        data = _jload(text)
        if data == 'Illegal':
            raise ToolError(
                '接口不存在。Yearning 对错误子路径的兜底响应是字符串 "Illegal"，'
                '不是报错。\n路径: %s' % path)
        return status, data

    def get_json(path):
        """只读 GET，返回 ``(status, payload)``。"""
        status, data = do('GET', path)
        return status, _payload_of(data)

    def act(action, body=None, path=None):
        """按白名单发一个写动作，返回 ``(status, data)``。"""
        method, default_path = safety.check_write(action)
        return do(method, path or default_path, body)

    def action_ok(action, data):
        """判断写动作是否被 Yearning 接受（业务码 1200）。"""
        if isinstance(data, dict):
            code = data.get('code')
            if code is not None and code != 1200:
                raise ToolError('Yearning 拒绝了 %s：code=%s %s'
                                % (action, code,
                                   data.get('text') or data.get('msg') or ''))
            return data.get('text') or data.get('msg') or '已完成'
        if isinstance(data, str):
            return data
        return _pretty(data)

    # ------------------------------------------------------------------ 门禁

    def fetch_sources(idc, tp):
        """取某环境某类型下我可用的数据源与可选审核人。

        返回 ``(sources, assigned)``。注意 Yearning 对空参数直接 ``return``，
        所以 ``idc`` 与 ``tp`` 必须都给。
        """
        status, data = do('GET', '/api/v2/fetch/source?idc=%s&tp=%s'
                          % (_qs(idc), _qs(tp)))
        payload = _payload_of(data)
        if not isinstance(payload, dict):
            # 把服务端原话带出来 —— 「环境没有添加流程!无法提交工单」这类提示
            # 才是用户真正需要看到的原因，笼统的「读取失败」没有用。
            server_msg = ''
            if isinstance(data, dict):
                server_msg = data.get('text') or data.get('msg') or ''
            raise ToolError(
                '读取 %s 环境下 %s 的可用数据源失败。\n'
                '  HTTP     : %s\n'
                '  服务端说 : %s\n'
                '  返回体   : %s\n\n'
                '常见原因：该环境没有配置审批流程（服务端会提示「环境没有添加流程」），'
                '或者你的账号没有任何权限组。'
                % (idc, tp, status, server_msg or '(无提示语)',
                   _clip(_pretty(data), 200)))
        return list(payload.get('source') or []), list(payload.get('assigned') or [])

    def gate_source(idc, tp, source):
        """闸门 1：数据源必须在该环境该类型的授权清单里。

        这是 MCP 补上的关键校验 —— Yearning 服务端提工单时不做任何权限判断。
        """
        allowed, assigned = fetch_sources(idc, tp)
        exact = writes.resolve_source(allowed, source)
        if exact is None:
            raise ToolError(writes.missing_source_hint(
                allowed, idc, {'ddl': 'DDL', 'dml': 'DML',
                               'query': '查询'}.get(tp, tp)))
        return exact, assigned, allowed

    def gate_assigned(assigned_list, assigned):
        """闸门 2：审核人必须来自服务端给出的可选清单。"""
        want = str(assigned or '').strip()
        if not want:
            if not assigned_list:
                raise ToolError(
                    '该环境的审批流程里没有可选的审核人，无法确定 assigned 字段。\n'
                    '请让管理员检查该环境的流程配置。')
            raise ToolError(
                '必须指定审核人 assigned。该环境下可选的审核人：\n%s'
                % '\n'.join('  - %s' % a for a in assigned_list))
        for item in assigned_list:
            if str(item).strip() == want:
                return item
        raise ToolError(
            '审核人 %s 不在该环境的可选清单里。可选：\n%s'
            % (want, '\n'.join('  - %s' % a for a in assigned_list) or '  （空）'))

    # ------------------------------------------------------------------ 只读工具

    def t_status(_args):
        def lab(label):
            # 中文标签按显示宽度补齐，否则 '执行前备份' 这类会和 '环境' 错位
            return _pad(label, 8) + ' : '

        lines = [lab('配置来源') + str(config_source),
                 lab('目标实例') + str(client.base)]

        reachable, err = client.tcp_ok()
        lines.append(lab('TCP 连通') + ('可达' if reachable else '不可达 -> ' + err))
        if not reachable:
            lines.append('')
            lines.append('内网地址，请先连上 VPN 再试。')
            return '\n'.join(lines)

        try:
            client.ensure_login()
        except (ConnectionFailed, LoginFailed) as e:
            lines.append(lab('登录') + '失败')
            lines.append('')
            lines.append(str(e))
            return '\n'.join(lines)

        claims = client.claims
        left = client.token_seconds_left()
        lines.append(lab('登录方式') + str(client.login_api or '(未知)'))
        lines.append(lab('账号') + str(claims.get('name')))
        lines.append(lab('角色') + str(claims.get('role')))
        lines.append(lab('真实姓名') + str(client.realname or '(登录响应里没有)'))
        lines.append(lab('token') + '剩余 %d 小时 %d 分'
                     % (left // 3600, left % 3600 // 60))

        # 真实提工单权限只能用 fetch/source 挨个环境问出来。
        # manage/group 返回的空数组是误导性的（权限合并逻辑不是同一套）。
        try:
            _status, idcs = get_json('/api/v2/fetch/idc')
        except ToolError as e:
            lines.append('环境列表 : 读取失败 -> %s' % e)
            return '\n'.join(lines)

        if not isinstance(idcs, list) or not idcs:
            lines.append('环境列表 : 为空')
            return '\n'.join(lines)

        lines.append('')
        lines.append('各环境可提工单的数据源（实测 fetch/source）：')
        for idc in idcs:
            row = ['  %-22s' % idc]
            for tp in ('ddl', 'dml', 'query'):
                try:
                    allowed, _as = fetch_sources(idc, tp)
                    row.append('%s=%d' % (tp, len(allowed)))
                except ToolError:
                    row.append('%s=?' % tp)
            lines.append('  '.join(row))

        lines.append('')
        lines.append('注：manage/group 返回空数组**不代表没有权限** —— 那个接口的')
        lines.append('    权限合并逻辑与 fetch/source 不是一套，别用它判断能不能提工单。')
        lines.append('    要具体清单用 yearning_allowed_sources。')
        return '\n'.join(lines)

    def t_allowed_sources(args):
        idc = str(args.get('idc') or '').strip()
        tp = str(args.get('type') or 'all').strip().lower()
        if not idc:
            raise ToolError('必须提供 idc（环境名）。可用环境见 yearning_list_environments。')
        if tp not in SOURCE_TYPES:
            raise ToolError('type 只能是 %s 之一，收到 %r'
                            % ('/'.join(SOURCE_TYPES), tp))

        allowed, assigned = fetch_sources(idc, tp)
        lines = ['环境 %s / 类型 %s' % (idc, tp),
                 '',
                 '可提工单的数据源（共 %d 个）：' % len(allowed)]
        if allowed:
            for item in allowed:
                # 线上真的存在 " 名字" 这种带前导空格的记录，这里标出来
                raw = str(item)
                mark = '   （注意原名含前后空格）' if raw != raw.strip() else ''
                lines.append('  - %s%s' % (raw, mark))
        else:
            lines.append('  （空 —— 该环境下你没有这个类型的权限）')

        lines.append('')
        lines.append('可选审核人 assigned（共 %d 个）：' % len(assigned))
        if assigned:
            for item in assigned:
                lines.append('  - %s' % item)
        else:
            lines.append('  （空 —— 该环境的审批流程没配审核人）')
        return '\n'.join(lines)

    def t_system_stats(_args):
        _status, payload = get_json('/api/v2/dash/count')
        if not isinstance(payload, dict):
            return _pretty(payload)
        return ('Yearning 平台统计\n'
                '  用户数   : %s\n'
                '  工单总数 : %s\n'
                '  查询总数 : %s\n'
                '  数据源数 : %s'
                % (payload.get('createUser'), payload.get('order'),
                   payload.get('query'), payload.get('source')))

    def t_environments(_args):
        _status, payload = get_json('/api/v2/fetch/idc')
        if isinstance(payload, list):
            if not payload:
                return '环境(IDC)列表为空。'
            return '环境(IDC)列表，共 %d 个:\n%s' % (
                len(payload), '\n'.join('  - %s' % x for x in payload))
        return _pretty(payload)

    def t_datasource_usage(_args):
        _status, payload = get_json('/api/v2/dash/pie')
        if isinstance(payload, list) and payload:
            total = sum(int(x.get('count') or 0) for x in payload
                        if isinstance(x, dict))
            rows = []
            for item in payload[:30]:
                if not isinstance(item, dict):
                    continue
                name = item.get('data_base') or item.get('dataBase') or '?'
                cnt = int(item.get('count') or 0)
                pct = (cnt * 100.0 / total) if total else 0
                rows.append('  %-28s %8d  %5.1f%%' % (name, cnt, pct))
            head = '数据源查询量分布（共 %d 个数据源，%d 次查询）:\n' % (
                len(payload), total)
            return head + '\n'.join(rows)
        return '数据源查询量分布:\n%s' % _pretty(payload)

    def t_board(_args):
        _status, payload = get_json('/api/v2/fetch/board')
        return '首页公告板配置:\n%s' % _pretty(payload)

    def _int_arg(args, key, default):
        value = args.get(key)
        # 注意别写成 `value or default` —— 0 是 falsy，会把 pagesize=0 静默改成默认值
        if value in (None, ''):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ToolError('%s 必须是整数，收到 %r' % (key, value))

    def t_orders(args):
        page = _int_arg(args, 'page', 1)
        pagesize = _int_arg(args, 'pagesize', 10)
        if page < 1:
            page = 1
        if pagesize < 1 or pagesize > 200:
            raise ToolError('pagesize 需要在 1~200 之间，收到 %s' % pagesize)

        query = ['page=%d' % page, 'pagesize=%d' % pagesize]
        for key in ('idc', 'source', 'start_time', 'end_time'):
            value = args.get(key)
            if value not in (None, ''):
                query.append('%s=%s' % (key, _qs(value)))
        status, payload = get_json('/api/v2/fetch/detail?' + '&'.join(query))

        if isinstance(payload, dict) and 'record' in payload:
            count = payload.get('count')
            records = payload.get('record') or []
            head = '工单列表 (page=%d, pagesize=%d) —— 命中 %s 条，本页 %d 条:' % (
                page, pagesize, count, len(records))
            if not records:
                head += ('\n  （空。该接口按 work_id 查明细，需配合 work_id 使用；'
                         '查“我提过的工单”请用 yearning_my_orders。）')
            return head + '\n' + _pretty(payload)
        return '工单列表 (page=%d, pagesize=%d), HTTP %s:\n%s' % (
            page, pagesize, status, _pretty(payload))

    def t_api_get(args):
        path = str(args.get('path') or '').strip()
        reason = safety.check_get_path(path)
        if reason:
            raise ToolError(reason + safety.blocked_hint())
        status, payload = get_json(path)
        return 'GET %s -> HTTP %s\n%s' % (path, status, _pretty(payload))

    # ------------------------------------------------------------------ 我提的工单

    def t_my_orders(args):
        page = _int_arg(args, 'page', 1)
        if page < 1:
            page = 1

        try:
            since, until = args.get('since'), args.get('until')
            picker = None if (since in (None, '') and until in (None, '')) \
                else [since, until]
            find = writes.order_list_find(status=args.get('status'),
                                          text=args.get('text'),
                                          picker=picker)
        except ValueError as e:
            raise ToolError(str(e))

        status, data = act('my_orders', {'page': page, 'find': find, 'tp': ''})
        payload = _payload_of(data)
        if not isinstance(payload, dict):
            return 'PUT /api/v2/common/list -> HTTP %s\n%s' % (status, _pretty(data))

        records = payload.get('data') or []
        total = payload.get('page')
        type_names = {0: 'DDL', 1: 'DML'}
        lines = ['我提交的工单 —— 命中 %s 条，本页 %d 条:' % (total, len(records)),
                 '  %s' % writes.describe_my_orders_filter(find, page)]
        if not records:
            lines.append('')
            lines.append('  （空。若这个筛选条件下本不该为空，把 status 换成'
                         ' 7 / 全部 再查一次 —— 默认就是全部，除非你显式指定了别的。）')
            return '\n'.join(lines)

        def item(label, value):
            return '  %s: %s' % (_pad(label, 9), value)

        def state_text(code):
            try:
                return writes.status_label(code)
            except (TypeError, ValueError):
                return str(code)

        for r in records:
            if not isinstance(r, dict):
                continue
            lines.append('')
            lines.append(item('work_id', r.get('work_id')))
            lines.append(item('类型', '%s   状态: %s' % (
                type_names.get(r.get('type'), r.get('type')),
                state_text(r.get('status')))))
            lines.append(item('环境/源', '%s / %s' % (r.get('idc'), r.get('source'))))
            lines.append(item('库/表', '%s / %s' % (r.get('data_base'),
                                                   r.get('table') or '-')))
            lines.append(item('说明', (r.get('text') or '').strip()))
            lines.append(item('审核人', '%s   提交: %s' % (r.get('assigned'),
                                                        r.get('date'))))

        shown = page * writes.MY_ORDERS_PAGE_SIZE
        if isinstance(total, int) and total > shown:
            lines.append('')
            lines.append('还有 %d 条没显示，加大 page 继续翻，'
                         '或用 status / text / since / until 收窄。' % (total - shown))
        lines.append('')
        lines.append('提示：撤销「审核中」的工单用 yearning_revoke(target="order", '
                     'work_id=...)。')
        return '\n'.join(lines)

    def t_query_status(_args):
        status, data = act('query_status')
        payload = _payload_of(data)
        if not isinstance(payload, dict):
            return 'PUT /api/v2/query/status -> HTTP %s\n%s' % (status, _pretty(data))

        state = payload.get('status')
        meaning = {1: '已生效 —— 可以直接执行查询',
                   2: '待审核 —— 需要管理员批准',
                   3: '已结束 / 已过期 —— 需重新提交查询工单'}.get(state, '未知状态')

        def item(label, value):
            return '  %s: %s' % (_pad(label, 6), value)

        lines = ['我的查询工单状态：',
                 item('状态', '%s（%s）' % (state, meaning)),
                 item('环境', payload.get('idc')),
                 item('可导出', '是' if payload.get('export') else '否')]
        if state == 1:
            lines.append('')
            lines.append('现在可以用 yearning_run_query 执行只读查询。')
            lines.append('（注意一个上游行为：这个接口每次调用都会检查窗口是否过期，')
            lines.append('  过期就顺手把状态改成 3，但返回体里给的是**改动前的旧值**。')
            lines.append('  所以偶尔会看到「第一次显示已生效、再查变成已结束」——')
            lines.append('  那说明窗口刚好在你第一次调用时被判过期，不是工具抽风。）')
        elif state == 2:
            lines.append('')
            lines.append('等管理员批准后才能查询。')
        else:
            lines.append('')
            lines.append('用 yearning_submit_query_order 提交查询工单。')
        return '\n'.join(lines)

    # ------------------------------------------------------------------ 执行查询

    def _render_result(payload):
        if not isinstance(payload, dict):
            return _pretty(payload)
        titles = payload.get('title') or []
        rows = payload.get('data') or []
        cols = [t.get('title') if isinstance(t, dict) else str(t) for t in titles]

        lines = ['  耗时 : %s ms' % payload.get('time'),
                 '  行数 : %s' % payload.get('total'),
                 '  列   : %s' % (', '.join(cols) if cols else '(无)')]
        if payload.get('status'):
            lines.append('  注意 : 查询权限已到期，服务端返回了结束状态，'
                         '未执行 SQL。请重新提交查询工单。')
            return '\n'.join(lines)

        if not rows:
            lines.append('  （无数据行）')
            return '\n'.join(lines)

        lines.append('')
        shown = rows[:MAX_RENDER_ROWS]
        for row in shown:
            if isinstance(row, dict):
                values = [str(row.get(c, '')) for c in cols] if cols \
                    else [str(v) for v in row.values()]
            else:
                values = [str(row)]
            lines.append('  ' + ' | '.join(values))
        if len(rows) > len(shown):
            lines.append('  ...（共 %d 行，只渲染前 %d 行）'
                         % (len(rows), MAX_RENDER_ROWS))
        return '\n'.join(lines)

    def t_run_query(args):
        source = args.get('source')
        data_base = args.get('data_base')
        sql = args.get('sql')

        for key, value in (('source', source), ('data_base', data_base), ('sql', sql)):
            if not str(value or '').strip():
                raise ToolError('必须提供 %s' % key)

        # 闸门 A：SQL 必须全是只读语句
        reason = writes.ensure_read_only(sql)
        if reason:
            raise ToolError(reason)

        # 闸门 B：查询通道必须先确认有生效中的查询工单，并拿到它的环境
        _status, qs = act('query_status')
        qs = _payload_of(qs)
        if not isinstance(qs, dict):
            raise ToolError('读取查询工单状态失败：%s' % _pretty(qs))
        if qs.get('status') != 1:
            raise ToolError(
                '当前没有生效中的查询工单（状态 %s），无法执行查询。\n'
                'Yearning 的查询流程是：先提查询工单 → 管理员批准 → 有效期内执行。\n'
                '用 yearning_submit_query_order 提交，再用 yearning_query_status 看进度。'
                % qs.get('status'))
        idc = qs.get('idc')
        if not idc:
            raise ToolError('查询工单里没有环境信息，无法校验数据源归属，已中止。')

        # 闸门 C：数据源必须属于该查询工单的环境
        # 上游 query/results 只看「有没有生效工单」，不校验数据源属于哪个环境 ——
        # 批准了 A 环境照样能查 B 环境的库，这个洞由这里补上。
        exact, _assigned, allowed = gate_source(idc, 'query', source)

        status, data = act('run_query',
                           writes.query_body(exact, data_base, sql))
        if status != 200:
            raise ToolError('查询失败：HTTP %s\n%s' % (status, _pretty(data)))
        payload = _payload_of(data)
        if isinstance(data, dict) and data.get('code') not in (None, 1200):
            raise ToolError('查询被拒绝：code=%s %s\n%s' % (
                data.get('code'), data.get('text') or '', _pretty(data)))

        def item(label, value):
            return '  %s: %s' % (_pad(label, 8), value)

        head = ['查询已执行（只读）',
                item('环境', idc),
                item('数据源', exact),
                item('库', data_base),
                item('SQL', _clip(sql, 200))]
        return '\n'.join(head) + '\n' + _render_result(payload)

    # ------------------------------------------------------------------ 提交工单

    def _preview_order(fields, gates, action_hint):
        lines = ['【工单预览 —— 尚未提交】']
        for key, value in fields:
            lines.append('  %s: %s' % (_pad(key, 10), value))
        lines.append('')
        lines.append('【闸门检查】')
        for item in gates:
            lines.append('  ✓ %s' % item)
        lines.append('')
        lines.append('确认提交：再调一次，带上 confirm=true（%s）。' % action_hint)
        lines.append('想改内容就直接改参数重调，这一步不会产生任何工单。')
        return '\n'.join(lines)

    def t_submit_order(args):
        idc = str(args.get('idc') or '').strip()
        source = str(args.get('source') or '').strip()
        data_base = str(args.get('data_base') or '').strip()
        sql = args.get('sql')
        type_raw = str(args.get('type') or '').strip().lower()

        # ---- 本地校验全部先跑完，再碰网络。参数错就不该先打一次线上。 ----
        if not idc:
            raise ToolError('必须提供 idc（环境名）')
        if not source:
            raise ToolError('必须提供 source（数据源名）')
        if not data_base:
            raise ToolError('必须提供 data_base（库名）')
        if not str(sql or '').strip():
            raise ToolError('必须提供 sql')

        if type_raw in ('ddl', '0'):
            type_code, tp = 0, 'ddl'
        elif type_raw in ('dml', '1'):
            type_code, tp = 1, 'dml'
        else:
            raise ToolError('type 只能是 ddl 或 dml，收到 %r' % (args.get('type'),))

        text = str(args.get('text') or '').strip()
        if not text:
            raise ToolError('必须提供 text（工单说明）—— 审核人靠它判断你要干什么，'
                            '不允许留空。')

        # 闸门 3：SQL 与工单类型自洽（本地）
        reason = writes.check_order_type(sql, type_code)
        if reason:
            raise ToolError(reason)

        table = str(args.get('table') or '').strip()
        backup = 1 if args.get('backup') in (None, '') else (
            1 if _truthy(args.get('backup')) else 0)
        delay = str(args.get('delay') or 'none').strip() or 'none'

        # ---- 以下才会发请求 ----
        # 闸门 1：数据源在授权清单内（服务端不校验，这里补上）
        exact, assigned_list, _allowed = gate_source(idc, tp, source)
        # 闸门 2：审核人合法
        assigned = gate_assigned(assigned_list, args.get('assigned'))

        body = writes.order_payload(idc, exact, data_base, sql, type_code,
                                    text, table=table, backup=backup,
                                    delay=delay, assigned=assigned)

        if not _truthy(args.get('confirm')):
            return _preview_order(
                [('环境', idc), ('数据源', exact), ('库', data_base),
                 ('表', table or '(空)'),
                 ('类型', '%s（type=%d）' % (tp.upper(), type_code)),
                 ('审核人', assigned), ('延迟执行', delay),
                 ('执行前备份', '是' if backup else '否'),
                 ('说明', text),
                 ('SQL', '\n' + ' ' * 12 + ('\n' + ' ' * 12).join(
                     [s.strip() for s in str(sql).split(';') if s.strip()]))],
                ['数据源在 %s 的 %s 授权清单内' % (idc, tp.upper()),
                 '审核人 %s 在可选清单内' % assigned,
                 'SQL 与工单类型自洽（类型校验通过）'],
                '工具 yearning_submit_order')

        status, data = act('submit_order', body)
        if status != 200:
            raise ToolError('提交失败：HTTP %s\n%s' % (status, _pretty(data)))

        def item(label, value):
            return '  %s: %s' % (_pad(label, 12), value)

        return ('工单已提交\n'
                + item('Yearning 返回', action_ok('submit_order', data)) + '\n'
                + item('环境/数据源', '%s / %s' % (idc, exact)) + '\n'
                + item('库/表', '%s / %s' % (data_base, table or '-')) + '\n'
                + item('类型', tp.upper()) + '\n'
                + item('审核人', assigned) + '\n\n'
                '用 yearning_my_orders 查看进度；'
                '未开始的工单可以用 yearning_revoke 撤销。')

    def t_submit_query_order(args):
        idc = str(args.get('idc') or '').strip()
        text = str(args.get('text') or '').strip()
        if not idc:
            raise ToolError('必须提供 idc（环境名）—— 查询权限是按环境申请的。')
        if not text:
            raise ToolError('必须提供 text（申请说明）—— 审批人靠它判断是否批准。')

        # 闸门：该环境下必须有可查的数据源，否则批了也查不了
        _allowed, assigned_list = fetch_sources(idc, 'query')
        export = 1 if _truthy(args.get('export')) else 0
        body = writes.query_order_payload(idc, text, export=export)

        if not _truthy(args.get('confirm')):
            return _preview_order(
                [('环境', idc), ('说明', text),
                 ('允许导出结果', '是' if export else '否'),
                 ('审核人清单', '、'.join(assigned_list) or '(环境流程未配审核人)')],
                ['该环境下你有查询权限的数据源数量：%d' % len(_allowed)],
                '工具 yearning_submit_query_order')

        status, data = act('submit_query_order', body)
        if status != 200:
            raise ToolError('提交失败：HTTP %s\n%s' % (status, _pretty(data)))

        def item(label, value):
            return '  %s: %s' % (_pad(label, 12), value)

        return ('查询工单已提交\n'
                + item('Yearning 返回', action_ok('submit_query_order', data)) + '\n'
                + item('环境', idc) + '\n'
                + item('允许导出', '是' if export else '否') + '\n\n'
                '用 yearning_query_status 查看状态；'
                '状态为 1 时即可用 yearning_run_query 查询。')

    def t_revoke(args):
        target = str(args.get('target') or '').strip().lower()
        if target not in ('order', 'query'):
            raise ToolError('target 只能是 order（撤销工单）或 query（结束查询权限），'
                            '收到 %r' % (args.get('target'),))

        if target == 'order':
            work_id = str(args.get('work_id') or '').strip()
            if not work_id:
                raise ToolError('撤销工单必须提供 work_id（见 yearning_my_orders）。')
            label = '撤销工单 %s' % work_id
            action, path, body = 'cancel_order', \
                '/api/v2/fetch/undo?work_id=%s' % _qs(work_id), None
        else:
            label = '结束我自己的查询权限'
            action, path, body = 'end_query', None, None

        if not _truthy(args.get('confirm')):
            return _preview_order([('动作', label)], ['该动作只作用于你自己的数据'],
                                  '工具 yearning_revoke')

        status, data = act(action, body, path=path)
        if status != 200:
            raise ToolError('操作失败：HTTP %s\n%s' % (status, _pretty(data)))
        return '%s\n  Yearning 返回 : %s' % (label, action_ok(action, data))

    return [
        {
            'name': 'yearning_status',
            'description': ('检查 Yearning 平台连通性与当前身份：TCP 可达性、登录方式、'
                            '账号角色、真实姓名、token 剩余时间，并实测各环境下'
                            '「可提工单的数据源数量」（真实权限）。排查连接或权限问题时先调这个。'),
            'inputSchema': {'type': 'object', 'properties': {}},
            'handler': t_status,
        },
        {
            'name': 'yearning_allowed_sources',
            'description': ('查某个环境下、某个类型下我实际可用的数据源清单，'
                            '以及可选审核人。提工单前用它确认能提哪些库。'
                            '实测来源 GET /api/v2/fetch/source —— 这是唯一能反映'
                            '真实提工单权限的接口。'),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'idc': {'type': 'string', 'description': '环境名，见 yearning_list_environments'},
                    'type': {'type': 'string',
                             'description': 'ddl / dml / query / all，默认 all'},
                },
                'required': ['idc'],
            },
            'handler': t_allowed_sources,
        },
        {
            'name': 'yearning_system_stats',
            'description': 'Yearning 平台整体统计：用户数、工单总数、查询总数、数据源数量。',
            'inputSchema': {'type': 'object', 'properties': {}},
            'handler': t_system_stats,
        },
        {
            'name': 'yearning_list_environments',
            'description': '列出 Yearning 上登记的环境(IDC)名称。',
            'inputSchema': {'type': 'object', 'properties': {}},
            'handler': t_environments,
        },
        {
            'name': 'yearning_datasource_usage',
            'description': '按数据源统计查询次数分布（前 30 个），可用于判断哪个业务库被查得最多。',
            'inputSchema': {'type': 'object', 'properties': {}},
            'handler': t_datasource_usage,
        },
        {
            'name': 'yearning_board',
            'description': '读取 Yearning 首页公告板配置内容。',
            'inputSchema': {'type': 'object', 'properties': {}},
            'handler': t_board,
        },
        {
            'name': 'yearning_orders',
            'description': ('按 work_id 读工单的执行明细（GET /api/v2/fetch/detail）。'
                            '想看「我提交过的工单列表」请用 yearning_my_orders。'),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'page': {'type': 'integer', 'description': '页码，从 1 开始，默认 1'},
                    'pagesize': {'type': 'integer',
                                 'description': '每页条数，1~200，默认 10'},
                    'idc': {'type': 'string', 'description': '按环境(IDC)过滤'},
                    'source': {'type': 'string', 'description': '按数据源过滤'},
                    'start_time': {'type': 'string', 'description': '开始时间'},
                    'end_time': {'type': 'string', 'description': '结束时间'},
                },
            },
            'handler': t_orders,
        },
        {
            'name': 'yearning_my_orders',
            'description': (
                '读取我提交过的 DDL/DML 工单列表（含 work_id、类型、状态、审核人）。'
                '默认返回全部状态，可用 status / text / since / until 收窄。'
                '状态码：0=已驳回、1=已执行、2=审核中、3=执行中、4=执行失败、'
                '5=待执行、7=全部（默认）。也可以直接传中文标签，例如 '
                'status="已驳回"。撤销工单前先用它拿 work_id。'),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'page': {'type': 'integer',
                             'description': '页码，从 1 开始，默认 1；每页 15 条'},
                    'status': {
                        'type': ['integer', 'string'],
                        'description': ('按状态筛选。默认 7=全部。'
                                        '0=已驳回 1=已执行 2=审核中 3=执行中 '
                                        '4=执行失败 5=待执行，也可写中文标签'),
                    },
                    'text': {'type': 'string',
                             'description': '按工单说明模糊匹配（LIKE %text%）'},
                    'since': {'type': 'string',
                              'description': ('起始日期 YYYY-MM-DD。'
                                              '注意只能给日期，不能带时分 —— '
                                              '服务端是拿日期列做字符串比较的')},
                    'until': {'type': 'string',
                              'description': '结束日期 YYYY-MM-DD，只给日期'},
                },
            },
            'handler': t_my_orders,
        },
        {
            'name': 'yearning_query_status',
            'description': ('查看我的查询工单状态：1=已生效可查询、2=待审核、3=已结束。'
                            '执行查询前先看这个。'),
            'inputSchema': {'type': 'object', 'properties': {}},
            'handler': t_query_status,
        },
        {
            'name': 'yearning_run_query',
            'description': ('执行一条只读 SQL 并返回结果（SELECT / SHOW / DESC / EXPLAIN）。'
                            '前置条件：必须有生效中的查询工单（用 yearning_query_status 确认），'
                            '且数据源必须属于该查询工单的环境。'
                            '写语句（INSERT/UPDATE/DELETE/DDL）、SELECT ... INTO OUTFILE、'
                            'LOAD_FILE 等一律拒绝 —— 那些要走 yearning_submit_order。'
                            '注意 Yearning 的查询权限有有效窗口，过期后需要重新提交查询工单。'),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'source': {'type': 'string', 'description': '数据源名，见 yearning_allowed_sources'},
                    'data_base': {'type': 'string', 'description': '库名'},
                    'sql': {'type': 'string', 'description': '只读 SQL 语句'},
                },
                'required': ['source', 'data_base', 'sql'],
            },
            'handler': t_run_query,
        },
        {
            'name': 'yearning_submit_order',
            'description': ('提交 DDL 或 DML 工单（进审核流程，不会立即执行）。'
                            '两步操作：先不带 confirm 调用拿预览，确认无误后带 confirm=true 再调一次。'
                            '三重闸门：数据源必须在你的授权清单内、审核人必须可选、'
                            'SQL 类型必须与工单类型自洽（SELECT 会被拒绝，请走查询工单）。'),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'idc': {'type': 'string', 'description': '环境名'},
                    'source': {'type': 'string', 'description': '数据源名，见 yearning_allowed_sources'},
                    'data_base': {'type': 'string', 'description': '库名'},
                    'sql': {'type': 'string', 'description': 'DDL 或 DML 语句，可多条以分号分隔'},
                    'type': {'type': 'string', 'description': 'ddl 或 dml'},
                    'text': {'type': 'string', 'description': '工单说明，必填且不能为空'},
                    'table': {'type': 'string', 'description': '涉及的表名，可选'},
                    'assigned': {'type': 'string', 'description': '审核人，必须来自 yearning_allowed_sources 给出的清单'},
                    'backup': {'type': 'boolean', 'description': '执行前是否备份，默认 true（仅 DML 有意义）'},
                    'delay': {'type': 'string', 'description': '定时执行时间，格式 "2026-01-02 15:04"，默认立即'},
                    'confirm': {'type': 'boolean', 'description': 'true 才真正提交；省略或 false 只返回预览'},
                },
                'required': ['idc', 'source', 'data_base', 'sql', 'type',
                             'text', 'assigned'],
            },
            'handler': t_submit_order,
        },
        {
            'name': 'yearning_submit_query_order',
            'description': ('提交查询工单（申请某个环境的查询权限，进审核流程）。'
                            '批准后该环境下的数据源可以通过 yearning_run_query 查询。'
                            '同样是两步：先预览，带 confirm=true 才提交。'),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'idc': {'type': 'string', 'description': '环境名'},
                    'text': {'type': 'string', 'description': '申请说明，必填'},
                    'export': {'type': 'boolean', 'description': '是否允许导出结果，默认 false'},
                    'confirm': {'type': 'boolean', 'description': 'true 才真正提交'},
                },
                'required': ['idc', 'text'],
            },
            'handler': t_submit_query_order,
        },
        {
            'name': 'yearning_revoke',
            'description': ('撤销我自己的东西。target="order" 需要 work_id，'
                            '撤销一个尚未开始的工单（等同 Yearning 网页上的撤销）；'
                            'target="query" 结束我自己的查询权限。'
                            '只作用于当前账号，不会碰到别人的数据。两步确认。'),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'target': {'type': 'string', 'description': 'order 或 query'},
                    'work_id': {'type': 'string', 'description': 'target=order 时必填'},
                    'confirm': {'type': 'boolean', 'description': 'true 才真正执行'},
                },
                'required': ['target'],
            },
            'handler': t_revoke,
        },
        {
            'name': 'yearning_api_get',
            'description': ('受控的只读接口透传：对 Yearning 发一个 GET /api/v2/* 请求，'
                            '用于读取上面未覆盖的只读接口。'
                            '非 GET、写操作型 GET（如 fetch/undo）、审批与管理类接口、'
                            '以及含密码哈希的接口都在服务端被硬拦截，无法绕过。'),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string',
                             'description': '形如 /api/v2/dash/count，可带查询串'},
                },
                'required': ['path'],
            },
            'handler': t_api_get,
        },
    ]


def public_tools(tools):
    """剥掉 handler，只留协议需要的字段 —— 用于 tools/list 与 --print-tools。"""
    return [{'name': t['name'], 'description': t['description'],
             'inputSchema': t['inputSchema']} for t in tools]
