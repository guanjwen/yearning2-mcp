# -*- coding: utf-8 -*-
"""命令行入口。

::

    yearning2-mcp                    启动 stdio MCP 服务（MCP 客户端默认这么调）
    yearning2-mcp --check-config     打印解析到的配置（不含密码明文）
    yearning2-mcp --selftest         对真实实例做连通/登录/权限自检
    yearning2-mcp --print-tools      以 JSON 打印工具清单（调试用）
    yearning2-mcp --version          版本号
"""
import argparse
import json
import sys
import time

from . import __version__, _log, safety
from . import tools as tools_mod
from .client import (ConnectionFailed, LoginFailed, YearningClient,
                     default_session_path)
from .config import ConfigError, describe, load_config
from .server import McpServer, StdoutSender


def _build_parser():
    p = argparse.ArgumentParser(
        prog='yearning2-mcp',
        description='Yearning 2.x 的 MCP 服务端（只读，零第三方依赖）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument('--config', metavar='PATH',
                   help='指定配置文件（优先级最高）')
    p.add_argument('--version', action='version',
                   version='yearning2-mcp %s' % __version__)
    p.add_argument('--check-config', action='store_true',
                   help='打印解析到的配置，不启动服务')
    p.add_argument('--selftest', action='store_true',
                   help='对真实实例自检连通性/登录/权限，不启动服务')
    p.add_argument('--report', metavar='PATH',
                   help='配合 --selftest，把报告同时写入文件')
    p.add_argument('--print-tools', action='store_true',
                   help='以 JSON 打印工具清单，不启动服务')
    p.add_argument('--log-file', metavar='PATH',
                   help='日志文件路径（默认 ~/.yearning/mcp.log，off 关闭）')
    return p


def _load(explicit_config):
    try:
        cfg, source = load_config(explicit=explicit_config)
    except ConfigError as e:
        print('配置错误：%s' % e, file=sys.stderr)
        return None, None
    return cfg, source


def cmd_check_config(explicit_config):
    cfg, source = _load(explicit_config)
    if cfg is None:
        return 2
    print(describe(cfg, source))
    print()
    print('日志文件 : %s' % (_log.log_file() or '(已关闭)'))
    print('会话缓存 : %s' % default_session_path())
    return 0


def cmd_print_tools(explicit_config):
    cfg = load_config(explicit=explicit_config, require=False)[0]
    client = YearningClient(cfg) if cfg.get('endpoint') else None
    if client is None:
        # 没有 endpoint 也允许列工具，用占位实例
        client = YearningClient({'endpoint': 'http://placeholder'})
    tools = tools_mod.build_tools(client, '(未连接)')
    print(json.dumps(tools_mod.public_tools(tools), ensure_ascii=False, indent=2))
    return 0


def cmd_selftest(explicit_config, report_path=None):
    lines = []

    def rec(tag, name, detail=''):
        text = '[%s] %s%s' % (tag, name, ('  -- ' + detail) if detail else '')
        lines.append(text)
        print(text)

    print('yearning2-mcp 自检  v%s' % __version__)
    print('=' * 62)

    try:
        cfg, source = load_config(explicit=explicit_config)
    except ConfigError as e:
        rec('FAIL', '配置载入', str(e).splitlines()[0])
        _write_report(report_path, lines, ok=False)
        return 2
    rec('PASS', '配置载入', source)

    client = YearningClient(cfg, logger=_log.log)
    rec('INFO', '目标实例', client.base)

    # 1) 拦截规则自查 —— 纯本地，不需要网络
    cases = [
        ('/api/v2/dash/count', True),
        ('/api/v2/manage/group', True),
        ('/api/v2/query/results', False),
        ('/api/v2/query/results/', False),
        ('/API/V2/Query/Results', False),
        ('/api/v2/fetch/perform', False),
        ('/api/v2/audit/order/1', False),
        ('/api/v2/manage/user', False),
        ('/api/v2/../../etc/passwd', False),
        ('http://evil.example/x', False),
        ('/other/prefix', False),
    ]
    bad = []
    for path, should_allow in cases:
        allowed = safety.check_get_path(path) is None
        if allowed != should_allow:
            bad.append('%s（期望 %s，实际 %s）'
                       % (path, '放行' if should_allow else '拒绝',
                          '放行' if allowed else '拒绝'))
    rec('PASS' if not bad else 'FAIL', '接口拦截规则',
        '11 条用例全部符合预期' if not bad else '；'.join(bad))

    # 2) 连通性
    reachable, err = client.tcp_ok()
    rec('PASS' if reachable else 'FAIL', 'TCP 连通性', err or '可达')
    if not reachable:
        rec('INFO', '提示', '内网地址，请先连上 VPN 再跑自检')
        _write_report(report_path, lines, ok=False)
        return 1

    # 3) 登录
    try:
        client.login(force=True)
        rec('PASS', '登录认证', client.login_api)
    except (ConnectionFailed, LoginFailed) as e:
        rec('FAIL', '登录认证', str(e).splitlines()[0])
        _write_report(report_path, lines, ok=False)
        return 1

    claims = client.claims
    left = client.token_seconds_left()
    rec('INFO', '身份', 'name=%s  role=%s' % (claims.get('name'), claims.get('role')))
    rec('INFO', '真实姓名', client.realname or '(登录响应里没有)')
    rec('PASS' if left > 0 else 'WARN', 'token 有效期',
        '剩余 %d 小时 %d 分' % (left // 3600, left % 3600 // 60))

    # 4) 权限组
    status, text = client.get('/api/v2/manage/group')
    try:
        group = json.loads(text).get('payload')
    except ValueError:
        group = None
    if isinstance(group, dict):
        keys = ('ddl_source', 'dml_source', 'auditor', 'query_source')
        empty = all(not group.get(k) for k in keys)
        rec('WARN' if empty else 'PASS', '我的权限组',
            '全部为空 —— 未分配任何权限，只能读统计类接口' if empty
            else json.dumps(group, ensure_ascii=False))
    else:
        rec('WARN', '我的权限组', 'HTTP %s %s' % (status, str(text)[:80]))

    # 5) 只读接口
    readonly = [('系统统计', '/api/v2/dash/count'),
                ('环境列表', '/api/v2/fetch/idc'),
                ('数据源分布', '/api/v2/dash/pie'),
                ('公告板', '/api/v2/fetch/board')]
    for name, path in readonly:
        code, body = client.get(path)
        ok = code == 200 and body.strip() != '"Illegal"'
        rec('PASS' if ok else 'WARN', '只读接口 %s' % name,
            '%s %s' % (code, body[:90].replace('\n', ' ')))

    # 6) 权限墙 —— 确认越权接口确实被服务端挡住
    code, body = client.get('/api/v2/manage/user')
    rec('PASS' if code in (401, 403) else 'WARN', '权限墙验证',
        'GET /api/v2/manage/user -> %s %s' % (code, str(body)[:60]))

    print('=' * 62)
    print('结论: 连接正常')
    _write_report(report_path, lines, ok=True)
    return 0


def _write_report(report_path, lines, ok):
    if not report_path:
        return
    try:
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write('yearning2-mcp 自检报告\n')
            f.write('版本: %s\n' % __version__)
            f.write('时间: %s\n' % time.strftime('%Y-%m-%d %H:%M:%S'))
            f.write('=' * 62 + '\n')
            f.write('\n'.join(lines) + '\n')
            f.write('结论: %s\n' % ('连接正常' if ok else '未通过'))
        print('报告已写入: %s' % report_path)
    except OSError as e:
        print('报告写入失败: %s' % e, file=sys.stderr)


def cmd_serve(explicit_config, log_file):
    if log_file:
        _log.set_log_file(None if log_file == 'off' else log_file)
    try:
        cfg, source = load_config(explicit=explicit_config)
    except ConfigError as e:
        # stderr 是安全的：协议跑在 stdout
        print('yearning2-mcp 启动失败：%s' % e, file=sys.stderr)
        return 2

    sender = StdoutSender()        # 先抓真实 stdout，再装陷阱
    client = YearningClient(cfg, logger=_log.log)
    server = McpServer(client, source, version=__version__, send=sender)
    _log.log('config source: %s  endpoint: %s' % (source, client.base))
    server.run()
    return 0


def main(argv=None):
    args = _build_parser().parse_args(argv)

    if args.check_config:
        return cmd_check_config(args.config)
    if args.print_tools:
        return cmd_print_tools(args.config)
    if args.selftest:
        return cmd_selftest(args.config, args.report)
    return cmd_serve(args.config, args.log_file)


if __name__ == '__main__':
    sys.exit(main())
