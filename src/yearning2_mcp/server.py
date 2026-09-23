# -*- coding: utf-8 -*-
"""MCP 服务端（stdio 传输，换行分隔 JSON-RPC 2.0）。

协议要点：

* 传输是 **newline-delimited JSON** —— 一条消息一行，不是 LSP 的
  ``Content-Length`` 分帧。这一点写错就是完全连不上。
* 必须实现：``initialize`` / ``notifications/initialized`` / ``tools/list`` /
  ``tools/call`` / ``ping``；``resources/list`` 与 ``prompts/list`` 返空数组；
  未知请求回 ``-32601``。
* ``initialize`` 的 ``protocolVersion`` 回显客户端给的值 —— 兼容性最好。

设计上刻意做成**无模块级全局状态**：:class:`McpServer` 是一次性实例，
``handle()`` 是纯函数式（返回响应靠 ``send`` 回调），所以能直接在测试里
用内存管道驱动。
"""
import json
import os
import sys

from . import _log, safety, tools as tools_mod

SERVER_NAME = 'yearning2-mcp'
SERVER_VERSION_FALLBACK = '0.0.0'
DEFAULT_PROTOCOL = '2024-11-05'

INSTRUCTIONS = (
    'Yearning（SQL 审核平台）2.x 接入。先调 yearning_status 确认连通性与真实权限。'
    '只读能力：统计、环境、数据源、公告板、工单明细。'
    '提交能力：提 DDL/DML 工单、提查询工单、执行只读查询（SELECT）、撤销自己的工单 —— '
    '这些都要先过闸门，且提工单类默认只返回预览，需要显式 confirm=true 才真提交。'
    '本服务端不提供任何审批能力，也不碰别人的数据。'
)


class StdoutSender(object):
    """把响应写到真实 stdout（二进制，避开 Windows 的 CRLF 转换）。

    必须在 :func:`yearning2_mcp._log.install_stdout_trap` **之前**构造，
    因为陷阱一装上 ``sys.stdout`` 就不再是协议通道了。
    """

    def __init__(self, stream=None):
        self._stream = stream if stream is not None else sys.stdout.buffer

    def __call__(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')
        self._stream.write(data)
        self._stream.flush()


class McpServer(object):
    """一个 MCP 会话。"""

    def __init__(self, client, config_source, version=SERVER_VERSION_FALLBACK,
                 send=None):
        self.client = client
        self.config_source = config_source
        self.version = version
        self.send = send or StdoutSender()
        self.tools = tools_mod.build_tools(client, config_source)
        self.tool_map = dict((t['name'], t) for t in self.tools)

    # ------------------------------------------------------------------ 收发

    def _result(self, rid, result):
        self.send({'jsonrpc': '2.0', 'id': rid, 'result': result})

    def _error(self, rid, code, message):
        self.send({'jsonrpc': '2.0', 'id': rid,
                   'error': {'code': code, 'message': message}})

    # ------------------------------------------------------------------ 分发

    def handle(self, msg):
        """处理一条消息。通知类不产生响应。"""
        if not isinstance(msg, dict):
            return
        method = msg.get('method')
        rid = msg.get('id')
        params = msg.get('params') or {}

        if method == 'initialize':
            requested = params.get('protocolVersion') or DEFAULT_PROTOCOL
            client_info = params.get('clientInfo') or {}
            _log.log('initialize <- %s protocol=%s' % (client_info.get('name'),
                                                       requested))
            self._result(rid, {
                'protocolVersion': requested,
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {'name': SERVER_NAME, 'version': self.version},
                'instructions': INSTRUCTIONS,
            })
            return

        if method in ('notifications/initialized', 'notifications/cancelled'):
            return

        if method == 'ping':
            self._result(rid, {})
            return

        if method == 'tools/list':
            self._result(rid, {'tools': tools_mod.public_tools(self.tools)})
            return

        if method == 'tools/call':
            self._call_tool(rid, params)
            return

        if method == 'resources/list':
            self._result(rid, {'resources': []})
            return

        if method == 'prompts/list':
            self._result(rid, {'prompts': []})
            return

        if rid is None:
            return          # 未知通知，安静忽略
        self._error(rid, -32601, 'Method not found: %s' % method)

    def _call_tool(self, rid, params):
        name = params.get('name')
        args = params.get('arguments') or {}
        tool = self.tool_map.get(name)
        if tool is None:
            self._result(rid, {'content': [{'type': 'text',
                                            'text': '未知工具: %s' % name}],
                               'isError': True})
            return

        _log.log('tools/call %s %s' % (
            name, json.dumps(args, ensure_ascii=False)[:200]))
        try:
            text = tool['handler'](args)
            is_error = False
        except tools_mod.ToolError as e:
            text, is_error = str(e), True
        except Exception as e:      # 兜底：任何异常都不该掀翻服务
            _log.log('工具 %s 异常: %r' % (name, e))
            import traceback
            _log.log(traceback.format_exc())
            text = '工具执行异常: %s: %s' % (type(e).__name__, e)
            is_error = True

        self._result(rid, {'content': [{'type': 'text', 'text': str(text)}],
                           'isError': is_error})

    # ------------------------------------------------------------------ 主循环

    def run(self, stdin=None, install_trap=True):
        """从 stdin 逐行读消息，直到对方关闭。"""
        stream = stdin if stdin is not None else sys.stdin.buffer
        if install_trap:
            _log.install_stdout_trap()
        _log.log('=== server start pid=%d version=%s rules=%d ===' % (
            os.getpid(), self.version,
            len(safety.blocked_summary()['exact'])))

        while True:
            try:
                raw = stream.readline()
            except Exception as e:
                _log.log('stdin 读取失败: %r' % (e,))
                break
            if not raw:
                break
            line = raw.decode('utf-8', 'replace').strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                _log.log('非法 JSON: %s' % line[:300])
                continue
            try:
                self.handle(msg)
            except Exception:
                import traceback
                _log.log('handle 异常:\n%s' % traceback.format_exc())
        _log.log('=== server exit ===')
