# -*- coding: utf-8 -*-
"""Yearning HTTP 客户端（只读）。

针对 Yearning **2.x**：

* 登录：``POST /ldap``（LDAP）或 ``POST /login``（本地账号），**不带 /api 前缀**
* 业务：``GET /api/v2/*``，鉴权头 ``Authorization: Bearer <JWT>``
* JWT 有效期 8 小时；payload 里只有 ``name`` / ``role`` / ``exp``，
  **真实姓名只存在于登录响应里** —— 所以登录后要单独缓存

本模块不依赖任何第三方库。
"""
import base64
import json
import os
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .config import ConfigError

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

DEFAULT_TIMEOUT = 25
SESSION_FILENAME = 'session.json'
SESSION_ENV = 'YEARNING_SESSION'

# 登录成功的业务码（Yearning 自己的约定）
CODE_OK = 1200


class YearningError(Exception):
    """本客户端所有异常的基类。"""


class ConnectionFailed(YearningError):
    """TCP 层就连不上（多为内网地址没连 VPN）。"""


class LoginFailed(YearningError):
    """能连上，但登录没通过。"""


def default_session_path():
    """会话缓存路径：``$YEARNING_SESSION`` > ``~/.yearning/session.json``。"""
    env = os.environ.get(SESSION_ENV)
    if env:
        return env
    return os.path.join(os.path.expanduser('~'), '.yearning', SESSION_FILENAME)


def jwt_claims(token):
    """解析 JWT 的 payload（**不校验签名**），只用于读 name / role / exp。"""
    try:
        seg = token.split('.')[1]
        seg += '=' * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg).decode('utf-8'))
    except Exception:
        return {}


class YearningClient(object):
    """只读 HTTP 客户端，带会话缓存与自动重登。"""

    def __init__(self, cfg, session_path=None, timeout=DEFAULT_TIMEOUT, logger=None):
        endpoint = (cfg or {}).get('endpoint')
        if not endpoint:
            raise ConfigError('缺少 endpoint')
        self.cfg = cfg
        self.base = endpoint.rstrip('/')
        self.timeout = timeout
        self.session_path = session_path or default_session_path()
        self.log = logger or (lambda _m: None)

        # 企业环境常见 HTTP_PROXY；内网地址必须显式绕开代理，否则一律超时。
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        self.token = ''
        self.claims = {}
        self.realname = ''
        self.permissions = ''
        self.login_api = ''
        self.last_error = ''
        self._load_session()

    # ------------------------------------------------------------------ 会话

    def _load_session(self):
        try:
            with open(self.session_path, encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            return
        if not isinstance(data, dict) or data.get('endpoint') != self.base:
            return          # 换了实例就不复用旧 token
        token = data.get('token') or ''
        claims = jwt_claims(token)
        if claims.get('exp', 0) <= time.time() + 30:
            return          # 快过期了，当没有
        self.token = token
        self.claims = claims
        self.realname = data.get('realname') or ''
        self.permissions = data.get('permissions') or ''
        if data.get('login_api'):
            self.login_api = data['login_api'] + ' (cached)'

    def _save_session(self):
        data = {
            'endpoint': self.base,
            'token': self.token,
            'realname': self.realname,
            'permissions': self.permissions,
            'login_api': (self.login_api or '').replace(' (cached)', ''),
            'saved_at': int(time.time()),
        }
        try:
            directory = os.path.dirname(self.session_path)
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            with open(self.session_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            try:
                os.chmod(self.session_path, 0o600)   # POSIX 收紧权限；Windows 会抛错，忽略
            except OSError:
                pass
        except Exception as e:
            self.log('会话缓存写入失败: %r' % (e,))

    def clear_session(self):
        try:
            os.remove(self.session_path)
        except OSError:
            pass
        self.token = ''
        self.claims = {}

    # ------------------------------------------------------------------ 请求

    def request(self, method, path, body=None, timeout=None, auth=True):
        """发一个请求，返回 ``(status, text)``；连不上时 status 为 None。"""
        url = self.base + path
        payload = json.dumps(body).encode('utf-8') if body is not None else None
        headers = {'User-Agent': USER_AGENT, 'Accept': '*/*'}
        if payload:
            headers['Content-Type'] = 'application/json; charset=UTF-8'
        if auth and self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as resp:
                return resp.status, resp.read().decode('utf-8', errors='replace')
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode('utf-8', errors='replace')
        except Exception as e:
            return None, 'CONN_ERROR: %s' % e

    def get(self, path, **kwargs):
        return self.request('GET', path, **kwargs)

    def login_endpoints(self):
        """按 login_mode 决定先试哪个登录接口。"""
        if str(self.cfg.get('login_mode') or '').lower() == 'local':
            return ('/login', '/ldap')
        return ('/ldap', '/login')

    def ensure_login(self):
        """确保有可用 token。失败抛 :class:`LoginFailed`；连不上抛 :class:`ConnectionFailed`。"""
        if self.token:
            return self
        return self.login()

    def login(self, force=False):
        """登录并缓存 token / 真实姓名 / 权限。失败抛异常。"""
        if self.token and not force:
            return self
        self.last_error = ''

        for path in self.login_endpoints():
            status, text = self.request(
                'POST', path,
                {'username': self.cfg.get('username'),
                 'password': self.cfg.get('password')},
                auth=False)

            if status is None:
                raise ConnectionFailed(
                    '无法连接 %s\n%s\n（内网地址请先连上 VPN；本客户端已绕开系统代理）'
                    % (self.base, text))
            if status != 200:
                self.last_error = '%s -> HTTP %s %s' % (path, status, text[:160])
                continue

            try:
                data = json.loads(text)
            except ValueError:
                self.last_error = '%s -> 响应不是 JSON: %s' % (path, text[:160])
                continue

            payload = data.get('payload') or {}
            token = payload.get('token') if isinstance(payload, dict) else ''
            if data.get('code') == CODE_OK and token:
                self.permissions = payload.get('permissions', '')
                self.realname = payload.get('real_name', '')
                self.login_api = path
                self.token = token
                self.claims = jwt_claims(token)
                self._save_session()
                return self

            self.last_error = '%s -> code=%s %s' % (
                path, data.get('code'), data.get('text') or data.get('msg') or '')

        raise LoginFailed(
            '登录失败：账号或密码错误，或 LDAP 不可达。\n'
            '最后一次尝试：%s\n'
            '账号：%s  密码长度：%s'
            % (self.last_error or '(无)',
               self.cfg.get('username'),
               len(str(self.cfg.get('password') or ''))))

    # ------------------------------------------------------------------ 探活

    def tcp_ok(self, timeout=6):
        """纯 TCP 探活，不涉及 HTTP —— 用来区分「网络不通」和「登录不通」。"""
        parsed = urlparse(self.base)
        host, port = parsed.hostname, parsed.port or 80
        sock = socket.socket()
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            return True, ''
        except Exception as e:
            return False, str(e)
        finally:
            sock.close()

    def token_seconds_left(self):
        exp = self.claims.get('exp', 0)
        return int(exp - time.time()) if exp else 0
