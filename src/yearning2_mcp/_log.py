# -*- coding: utf-8 -*-
"""日志与 stdout 保护。

两条铁律：

1. **stdio 模式下 stdout 就是协议通道**。任何一句 ``print`` 都会污染
   JSON-RPC 流、直接搞死连接。所以服务端启动时会把 ``sys.stdout`` 换成
   落盘陷阱，让所有误打印都进日志文件。
2. 日志一律落盘，不上屏（也上不了屏 —— 见第 1 条）。

日志文件位置：``$YEARNING_MCP_LOG`` > ``~/.yearning/mcp.log``
设为 ``off`` 可关闭日志。
"""
import io
import os
import sys
import time

MAX_LOG_BYTES = 2 * 1024 * 1024

_log_file = None
_log_resolved = False


def default_log_file():
    return os.path.join(os.path.expanduser('~'), '.yearning', 'mcp.log')


def log_file():
    """解析日志文件路径；返回 None 表示不写日志。"""
    global _log_file, _log_resolved
    if _log_resolved:
        return _log_file
    env = os.environ.get('YEARNING_MCP_LOG')
    if env is None:
        _log_file = default_log_file()
    elif env.strip().lower() in ('off', 'none', '0'):
        _log_file = None
    else:
        _log_file = env
    _log_resolved = True
    return _log_file


def set_log_file(path):
    """显式指定日志文件；传 None 关闭。"""
    global _log_file, _log_resolved
    _log_file = path
    _log_resolved = True


def log(msg):
    """写一条日志。任何异常都吞掉 —— 日志不该影响服务本身。"""
    path = log_file()
    if not path:
        return
    try:
        if os.path.exists(path) and os.path.getsize(path) > MAX_LOG_BYTES:
            try:
                os.replace(path, path + '.old')
            except OSError:
                pass
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write('%s  %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    except Exception:
        pass


class StdoutTrap(io.TextIOBase):
    """把误入 stdout 的输出转进日志，保护协议通道。"""

    def write(self, s):
        if s and s.strip():
            log('[stdout-trap] ' + s.rstrip())
        return len(s)

    def flush(self):
        pass


def install_stdout_trap():
    """用落盘陷阱替换 sys.stdout。必须在抓到真实输出通道之后调用。"""
    sys.stdout = StdoutTrap()
