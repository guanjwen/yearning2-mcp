# -*- coding: utf-8 -*-
"""进程内的假 Yearning 2.x 服务，供离线测试使用。

刻意复刻真实服务的几个怪癖，否则测试过了线上照样挂：

* 登录在**根路径** ``/ldap`` 与 ``/login``，不带 ``/api`` 前缀
* 错误子路径返回字符串 ``"Illegal"``（不是 404、不是报错）
* ``GET /api/v2/fetch/source`` **不带参数时返回空响应体**（源码里是 ``return``），
  前端 JSON 解析得到 ``null`` —— 我们早期就是被这条坑过
* ``fetch/source`` 返回的数据源名可能**带前后空格**（线上真有这种脏数据）
* ``/api/v2/fetch/perform`` 会返回密码哈希 —— 用来验证我们从不调它
* ``/api/v2/manage/user`` 对普通角色返回 403 非法越权操作

另外它会记录**每一个**收到的请求，测试靠这个断言高危接口没被碰过。
"""
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEMO_SOURCES = [
    {'data_base': 'demo_db', 'count': 30},
    {'data_base': 'order_db', 'count': 10},
]

DEMO_GROUP = {
    'ddl_source': [],
    'dml_source': [],
    'auditor': [],
    'query_source': [],
}

# 每个环境里登记的全部数据源（fetch/source?tp=all 返回的）
DEMO_ENV_SOURCES = {
    'env-demo-a': ['demo_db_src', ' db-readonly'],   # 第二个刻意带前导空格
    'env-demo-b': ['other_src'],
}

# 我的权限清单（fetch/source?tp=ddl|dml|query 返回的）
DEMO_PERMISSIONS = {
    'env-demo-a': {
        'ddl': ['demo_db_src'],
        'dml': ['demo_db_src'],
        'query': ['demo_db_src', ' db-readonly'],
    },
    'env-demo-b': {'ddl': [], 'dml': [], 'query': []},
}

DEMO_ASSIGNED = ['auditor-a', 'auditor-b']
DEMO_QUERY_ASSIGNED = ['auditor-a']

DEMO_MY_ORDERS = [
    {'work_id': '20260101120000123', 'username': 'tester', 'status': 2, 'type': 1,
     'backup': 1, 'idc': 'env-demo-a', 'source': 'demo_db_src',
     'data_base': 'demo_db', 'table': '', 'date': '2026-01-01 12:00',
     'sql': 'UPDATE t SET a=1;', 'text': '修数据', 'assigned': 'auditor-a',
     'delay': 'none', 'real_name': '测试用户', 'current_step': 1,
     'relevant': ['auditor-a']},
]

DEMO_QUERY_RESULT = {
    'title': [{'title': 'id', 'key': 'id', 'width': '200'},
              {'title': 'name', 'key': 'name', 'width': '200'}],
    'data': [{'id': '1', 'name': 'alpha'}, {'id': '2', 'name': 'beta'}],
    'status': False,
    'time': 3,
    'total': 2,
}


def _b64(data):
    raw = json.dumps(data, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def make_jwt(name, role='guest', ttl=8 * 3600):
    """造一个结构正确的 JWT。签名是假的 —— 客户端本来就不校验签名。"""
    header = _b64({'alg': 'HS256', 'typ': 'JWT'})
    payload = _b64({'exp': int(time.time()) + ttl, 'name': name, 'role': role})
    return '%s.%s.%s' % (header, payload, 'fake-signature')


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def log_message(self, fmt, *args):
        pass

    # -------------------------------------------------------------- 工具

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _empty(self):
        """返回 200 + 空响应体 —— 复刻 fetch/source 无参时的行为。"""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _read_body(self):
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        try:
            return json.loads(raw.decode('utf-8') or '{}')
        except ValueError:
            return {}

    def _authorized(self):
        header = self.headers.get('Authorization') or ''
        return header.startswith('Bearer ') and header[7:] == self.server.fake.token

    def _record(self):
        parsed = urlparse(self.path)
        body = self._read_body()
        self.server.fake.requests.append((self.command, self.path))
        if self.command != 'GET':
            self.server.fake.writes.append((self.command, parsed.path, body))
        return parsed, body

    # -------------------------------------------------------------- 路由

    def do_POST(self):
        parsed, body = self._record()
        fake = self.server.fake
        creds_ok = (body.get('username') == fake.username
                    and body.get('password') == fake.password)

        if parsed.path == '/ldap':
            if fake.ldap_enabled and creds_ok:
                self._json({'code': 1200, 'payload': fake.login_payload()})
            else:
                self._json({'code': 1301, 'text': '账号或密码错误'})
            return
        if parsed.path == '/login':
            if fake.local_enabled and creds_ok:
                self._json({'code': 1200, 'payload': fake.login_payload()})
            else:
                self._json({'code': 1301, 'text': '账号或密码错误'})
            return

        if not self._authorized():
            self._json({'code': 401, 'text': '未授权'}, status=401)
            return

        if parsed.path.startswith('/api/v2/common/'):
            # 提交 DDL/DML 工单 —— 真服务对任意 tp 都走同一个 handler
            self._json({'code': 1200, 'text': '工单已创建!'})
            return
        if parsed.path == '/api/v2/query/refer':
            if fake.query_order_duplicate:
                self._json({'code': 1200, 'text': '工单请勿重复提交!'})
            else:
                self._json({'code': 1200, 'text': '工单已创建!'})
            return
        if parsed.path == '/api/v2/query/results':
            if fake.query_state != 1:
                self._json({'code': 1200,
                            'payload': {'status': True}})
                return
            self._json({'code': 1200, 'payload': dict(DEMO_QUERY_RESULT)})
            return
        if parsed.path == '/api/v2/fetch/marge':
            self._json('Illegal')
            return
        self._json('Illegal')

    def do_GET(self):
        parsed, _body = self._record()
        fake = self.server.fake
        path = parsed.path

        if not self._authorized():
            self._json({'code': 401, 'text': '未授权'}, status=401)
            return

        if path == '/api/v2/dash/count':
            self._json({'code': 1200, 'payload': {'createUser': 3, 'order': 42,
                                                  'query': 7, 'source': 2}})
        elif path == '/api/v2/fetch/idc':
            self._json({'code': 1200, 'payload': list(DEMO_ENV_SOURCES)})
        elif path == '/api/v2/dash/pie':
            self._json({'code': 1200, 'payload': [dict(x) for x in DEMO_SOURCES]})
        elif path == '/api/v2/fetch/board':
            self._json({'code': 1200,
                        'payload': {'ID': 0, 'Authorization': '', 'Content': ''}})
        elif path == '/api/v2/fetch/detail':
            self._json({'code': 1200, 'payload': {'count': 0, 'record': []}})
        elif path == '/api/v2/fetch/source':
            self._source(parse_qs(parsed.query))
        elif path == '/api/v2/fetch/undo':
            work_id = (parse_qs(parsed.query).get('work_id') or [''])[0]
            if work_id:
                self._json('工单已撤销！')
            else:
                self._json('工单状态已更改！无法撤销')
        elif path == '/api/v2/manage/group':
            self._json({'code': 1200, 'payload': dict(DEMO_GROUP)})
        elif path == '/api/v2/fetch/perform':
            fake.sensitive_hits.append(path)
            self._json({'code': 1200, 'payload': {'perform': [
                {'id': 1, 'username': 'admin',
                 'password': 'pbkdf2_sha256$120000$demo'}]}})
        elif path == '/api/v2/manage/user':
            self._json({'code': 403, 'text': '非法越权操作'}, status=403)
        else:
            self._json('Illegal')

    def do_PUT(self):
        parsed, body = self._record()
        fake = self.server.fake
        if not self._authorized():
            self._json({'code': 401, 'text': '未授权'}, status=401)
            return

        if parsed.path == '/api/v2/common/list':
            self._json({'code': 1200,
                        'payload': {'page': len(DEMO_MY_ORDERS),
                                    'data': [dict(x) for x in DEMO_MY_ORDERS]}})
            return
        if parsed.path == '/api/v2/query/status':
            self._json({'code': 1200,
                        'payload': {'status': fake.query_state,
                                    'export': True,
                                    'idc': fake.query_idc}})
            return
        if parsed.path == '/api/v2/query/fetch_base':
            self._json({'code': 1200, 'payload': 0})
            return
        self._json('Illegal')

    def do_DELETE(self):
        parsed, _body = self._record()
        if not self._authorized():
            self._json({'code': 401, 'text': '未授权'}, status=401)
            return
        if parsed.path == '/api/v2/query':
            self._json({'code': 1200, 'text': '工单已终止'})
            return
        self._json('Illegal')

    # -------------------------------------------------------------- fetch/source

    def _source(self, query):
        """复刻 FetchSource：空参数直接 return（空响应体），有参数才返回 payload。"""
        idc = (query.get('idc') or [''])[0]
        tp = (query.get('tp') or [''])[0]
        if not idc and not tp:
            self._empty()
            return

        sources = DEMO_ENV_SOURCES.get(idc, [])
        if tp == 'all':
            picked = list(sources)
        elif tp in ('ddl', 'dml', 'query'):
            picked = list(DEMO_PERMISSIONS.get(idc, {}).get(tp, []))
        else:
            picked = []

        assigned = list(DEMO_QUERY_ASSIGNED if tp == 'query' else DEMO_ASSIGNED)
        self._json({'code': 1200,
                    'payload': {'assigned': assigned, 'source': picked}})


class FakeYearning(object):
    """一个跑在 127.0.0.1 随机端口上的假 Yearning。"""

    def __init__(self, username='tester', password='s3cret', role='guest',
                 realname='测试用户', ldap_enabled=True, local_enabled=False,
                 query_state=1, query_idc='env-demo-a',
                 query_order_duplicate=False):
        self.username = username
        self.password = password
        self.role = role
        self.realname = realname
        self.ldap_enabled = ldap_enabled
        self.local_enabled = local_enabled
        self.query_state = query_state
        self.query_idc = query_idc
        self.query_order_duplicate = query_order_duplicate
        self.token = make_jwt(username, role)
        self.requests = []          # [(method, path)] —— 全量留痕
        self.writes = []            # [(method, path, body)] —— 非 GET 的留痕
        self.sensitive_hits = []    # 只记 /api/v2/fetch/perform
        self.url = ''
        self._httpd = None
        self._thread = None

    def login_payload(self):
        return {'token': self.token, 'real_name': self.realname,
                'permissions': self.role}

    def start(self):
        self._httpd = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self._httpd.fake = self
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        self.url = 'http://%s:%d' % (host, port)
        return self.url

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------ 断言辅助

    def paths(self):
        """所有收到过的请求路径（去掉查询串）。"""
        return [p.split('?', 1)[0] for _m, p in self.requests]

    def called(self, path):
        return path in self.paths()

    def write_paths(self):
        """所有非 GET 请求的路径（登录除外）。"""
        return [p for _m, p, _b in self.writes if p not in ('/ldap', '/login')]

    def write_bodies(self, path):
        """某个写接口收到的全部请求体。"""
        return [b for _m, p, b in self.writes if p == path]
