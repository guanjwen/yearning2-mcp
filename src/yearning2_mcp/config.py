# -*- coding: utf-8 -*-
"""凭据与连接配置的载入。

查找顺序（命中即停）：

1. ``--config`` 指定的文件
2. 环境变量 ``YEARNING_CONFIG`` 指向的文件
3. ``~/.yearning/config.json``
4. ``./yearning.config.json``

在此之上，下列环境变量**始终优先**于文件里的同名字段：

===========================  ==========================================
``YEARNING_ENDPOINT``        Yearning 地址，如 ``http://yearning.corp:8000``
``YEARNING_USERNAME``        登录账号
``YEARNING_PASSWORD``        登录密码
``YEARNING_LOGIN_MODE``      ``ldap``（默认）或 ``local`` —— 决定先试哪个登录接口
===========================  ==========================================

配置文件格式（最小可用）：

.. code-block:: json

    {"endpoint": "http://yearning.example.com:8000",
     "username": "someone", "password": "******", "login_mode": "ldap"}
"""
import json
import os

ENV_ENDPOINT = 'YEARNING_ENDPOINT'
ENV_USERNAME = 'YEARNING_USERNAME'
ENV_PASSWORD = 'YEARNING_PASSWORD'
ENV_LOGIN_MODE = 'YEARNING_LOGIN_MODE'
ENV_CONFIG = 'YEARNING_CONFIG'

REQUIRED_KEYS = ('endpoint', 'username', 'password')

_ENV_MAP = (
    (ENV_ENDPOINT, 'endpoint'),
    (ENV_USERNAME, 'username'),
    (ENV_PASSWORD, 'password'),
    (ENV_LOGIN_MODE, 'login_mode'),
)


class ConfigError(Exception):
    """配置缺失或格式错误。"""


def normalize_endpoint(endpoint):
    """去掉尾部斜杠；缺协议时补 http://。"""
    ep = (endpoint or '').strip().rstrip('/')
    if ep and '://' not in ep:
        ep = 'http://' + ep
    return ep


def candidate_paths(explicit=None):
    """返回 [(来源标签, 路径), ...]，按优先级排列。"""
    out = []
    if explicit:
        out.append(('--config', explicit))
    env_path = os.environ.get(ENV_CONFIG)
    if env_path:
        out.append(('env:%s' % ENV_CONFIG, env_path))
    out.append(('~/.yearning/config.json',
                os.path.join(os.path.expanduser('~'), '.yearning', 'config.json')))
    out.append(('./yearning.config.json',
                os.path.join(os.getcwd(), 'yearning.config.json')))
    return out


def load_config(explicit=None, require=True):
    """载入配置。

    返回 ``(cfg, source)``；``source`` 是人类可读的来源描述，用于自检输出。
    缺必需字段且 ``require=True`` 时抛 :class:`ConfigError`。
    """
    cfg = {}
    source = '(未找到配置文件)'

    # --config 与 YEARNING_CONFIG 属于显式指定：文件不在就直接报错，
    # 不要悄悄回落到别的文件 —— 「明明配了却不生效」是最难查的一类问题。
    for label, path in candidate_paths(explicit):
        if not path:
            continue
        is_explicit = label.startswith('--config') or label.startswith('env:')
        if not os.path.isfile(path):
            if is_explicit:
                raise ConfigError('%s 指向的文件不存在：%s' % (label, path))
            continue
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except ValueError as e:
            raise ConfigError('配置文件不是合法 JSON：%s（%s）' % (path, e))
        except OSError as e:
            raise ConfigError('配置文件读不到：%s（%s）' % (path, e))
        if not isinstance(data, dict):
            raise ConfigError('配置文件顶层必须是 JSON 对象：%s' % path)
        cfg = dict(data)
        source = '%s %s' % (label, path)
        break

    overrides = []
    for env, key in _ENV_MAP:
        val = os.environ.get(env)
        if val:
            cfg[key] = val
            overrides.append(env)
    if overrides:
        source += '  +  env: ' + ', '.join(overrides)

    cfg['endpoint'] = normalize_endpoint(cfg.get('endpoint'))
    mode = str(cfg.get('login_mode') or '').strip().lower()
    cfg['login_mode'] = mode if mode in ('ldap', 'local') else 'ldap'

    if require:
        missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
        if missing:
            raise ConfigError(
                '缺少必需配置：%s。\n'
                '请二选一：\n'
                '  1) 设环境变量 %s / %s / %s\n'
                '  2) 写配置文件到 ~/.yearning/config.json\n'
                '（来源：%s）' % (', '.join(missing), ENV_ENDPOINT,
                                 ENV_USERNAME, ENV_PASSWORD, source))
    return cfg, source


def mask_secret(value):
    """打码：只留首尾各 1 位。用于打印配置时不泄露密码。"""
    s = str(value or '')
    if len(s) <= 2:
        return '*' * len(s)
    return s[0] + '*' * (len(s) - 2) + s[-1]


def describe(cfg, source):
    """人类可读的配置摘要 —— **不含密码明文**。"""
    lines = ['配置来源 : %s' % source,
             '目标实例 : %s' % (cfg.get('endpoint') or '(未设置)'),
             '登录账号 : %s' % (cfg.get('username') or '(未设置)'),
             '登录密码 : %s' % ('(已设置，长度 %d)' % len(cfg['password'])
                                if cfg.get('password') else '(未设置)'),
             '登录方式 : 先试 %s' % ('/login' if cfg.get('login_mode') == 'local'
                                     else '/ldap')]
    return '\n'.join(lines)
