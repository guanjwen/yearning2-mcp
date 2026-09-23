# Yearning 2.x 接口参考（实测整理）

本文是 `yearning2-mcp` 实现时整理的接口笔记，供二次开发参考。

**来源**：上游 [`cookieY/Yearning`](https://github.com/cookieY/Yearning) tag `2.3.5` 的
`src/router/router.go` 与各模块 `route.go`，以及 `src/handler/**` 里的请求结构体，
并在一个真实的企业内网部署（2.3.x + LDAP 认证）上逐条实测对齐。
文中已脱敏，不含任何真实地址、账号或库名。

---

## 通用约定

| 项 | 说明 |
|---|---|
| 登录接口 | `POST /ldap`（LDAP）· `POST /login`（本地账号）—— **不带 `/api` 前缀** |
| 业务接口 | `/api/v2/*` |
| 鉴权 | 请求头 `Authorization: Bearer <JWT>`，HMAC-SHA256 |
| JWT 有效期 | 8 小时。payload 只有 `{exp, name, role}` —— **真实姓名不在里面**，只在登录响应里 |
| 响应结构 | 统一 `{"code": 1200, "payload": ..., "text": ""}`，`code == 1200` 表示成功 |
| 子动作路由 | 大量接口是 `/api/v2/<模块>/:tp` 形式，`tp` 是子动作名 |

### 三个必须知道的坑

1. **`tp` 写错不会报错**，服务端返回字符串 `"Illegal"`（HTTP 200）。看到它说明路径写错了，
   不是接口挂了、也不是权限问题。
2. **登录响应里的 `real_name` 只在登录那一刻有。** 后续拿 JWT 去 parse 只能得到 `name` 和 `role`。
   想显示真实姓名就得像本项目一样，登录成功后单独缓存一份。
3. **`GET /api/v2/fetch/source` 空参数直接 `return`**，返回空响应体（`null`），不是报错。
   `idc` 与 `tp` 必须都给。

---

## 公开接口（无需 token）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/ldap` | LDAP 登录 |
| POST | `/login` | 本地账号登录 |
| POST | `/register` | 注册（是否开放取决于配置） |
| GET | `/fetch` | 读取系统开关（例如是否启用 LDAP） |

登录请求体：`{"username": "...", "password": "..."}`
登录响应：`{"code": 1200, "payload": {"token": "...", "real_name": "...", "permissions": "..."}}`

---

## 工单 `/api/v2/fetch/:tp`

| 方法 | tp | 说明 |
|---|---|---|
| GET | `idc` | 环境(IDC)列表，返回字符串数组 |
| GET | `board` | 公告板配置 |
| GET | `detail` | 工单执行明细分页，返回 `{"count": N, "record": [...]}` |
| GET | `roll` | 回滚 SQL |
| GET | `sql` | 工单 SQL 内容 |
| GET | `fields` | 表字段 |
| GET | `steps` | 流程步骤 |
| GET | `source` | 提交工单时可选的数据源与审核人，见下文。环境未配流程时返回 `code=5555 环境没有添加流程!无法提交工单` |
| GET | `undo` | **撤销工单**（写操作），参数 `?work_id=` —— 路径看着像只读，实际会删记录 |
| GET | `perform` | 执行人列表 ⚠️ **响应体含 `password` 字段（PBKDF2 哈希）**，见下文安全问题 |
| PUT | `test` · `merge` | SQL 测试 / 合并 DDL —— **会执行 SQL** |
| POST | `marge` · `roll_order` | 规则合并 / 回滚工单 —— **写操作** |

### `fetch/source` —— 唯一能反映真实提工单权限的接口

```
GET /api/v2/fetch/source?idc=<环境名>&tp=<ddl|dml|query>
```

响应体（`payload` 部分）：

```json
{
  "source":   ["db-prod", " db-readonly"],
  "assigned": ["reviewer-a", "reviewer-b"]
}
```

三点值得注意：

1. **这是提工单权限的唯一真实来源。** `manage/group` 返回的 `ddl_source` / `dml_source`
   是另一套合并逻辑，两者经常不一致 —— 别拿 `manage/group` 的空数组判断"没权限"。
2. **`tp` 传什么就查什么权限**：`ddl` / `dml` / `query`，三种清单可以完全不同。
3. **数据源名可能是脏的。** 线上存在 `" db-readonly"`（带前导空格）这类记录。
   Yearning 执行时是 `WHERE source = ?` **精确匹配**，所以比对要 strip、
   发回服务端时必须是数据库里的原样字符串。

---

## 提交工单 `/api/v2/common/:tp`

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v2/common/<tp>` | 提交 DDL/DML 工单 —— **handler 是 `SQLReferToOrder`** |
| PUT | `/api/v2/common/list` | 我提交过的工单列表 |

**`tp` 是摆设。** 路由注册成 `/api/v2/common/:tp`，但 handler 根本不读 `tp` ——
`common/order`、`common/anything` 走的是同一个 `SQLReferToOrder`。
本项目统一用 `common/order`，纯粹为了可读性。

请求体对应上游结构体 `model.CoreSqlOrder`，字段名必须与 json tag 一致：

```json
{
  "idc":       "env-prod",
  "source":    "db-prod",
  "data_base": "mydb",
  "table":     "users",
  "sql":       "ALTER TABLE users ADD COLUMN age INT;",
  "text":      "给 users 加个 age 字段",
  "type":      0,
  "backup":    1,
  "delay":     "none",
  "assigned":  "reviewer-a"
}
```

| 字段 | 取值 | 说明 |
|---|---|---|
| `type` | `0` / `1` | `0`=DDL，`1`=DML |
| `backup` | `0` / `1` | DML 是否先备份，对 DDL 无意义 |
| `delay` | `"none"` 或 `"2006-01-02 15:04"` | 定时执行时间；`"none"` 表示立即 |
| `assigned` | 审核人账号 | 服务端会写进 `relevant` 字段，必须是 `fetch/source` 给的 `assigned` 清单里的一员 |
| `sql` | 可多条，分号分隔 | 混写 DDL+DML 时按 DML 归类 |

⚠️ **这个接口在服务端没有任何权限校验。** `ddl_source` / `dml_source` 的过滤只发生在
`fetch/source` 里（也就是前端下拉框）。直接 POST 可以给任意数据源提工单 ——
调用方必须自己补闸门。

### `PUT /api/v2/common/list` —— 我提交过的工单

请求体是 `{page, find, tp}`，服务端（`personal.PersonalFetchMyOrder`）只读 `page`
与 `find`。`find` 的完整形状就是前端下拉框发的那份：

```json
{
  "page": 1,
  "find": {
    "picker": ["", ""], "valve": false, "text": "", "explain": "",
    "work_id": "", "type": 2, "status": 7, "source": "", "idc": "",
    "dept": "", "username": ""
  },
  "tp": ""
}
```

**真正生效的只有 3 个键**：`status`、`text`、`picker`
（分别走 `AccordingToAllOrderState` / `AccordingToText` / `AccordingToDatetime`）。
`type`、`source`、`idc`、`work_id`、`dept` 这个接口里没人读，是前端给别的列表页共用的。

| 键 | 行为 | 坑 |
|---|---|---|
| `status` | `7` = 不筛（全部）；其余走 `WHERE status = ?`，取值见下表 | **`find` 里不带 `status` 时 Go 零值是 `0`，等价于「只看已驳回」。** 实测线上 1165 条工单因此只显示了 2 条 —— 要全部就必须显式写 `7` |
| `text` | `text LIKE %…%`，匹配工单说明 | 空串等于不筛 |
| `picker` | `time >= 开始 AND time <= 结束`，**字符串比较** | `time` 列只存日期。传 `2026-09-23 00:00` 会让区间下界大于列值 → **恒返回空，且不报任何错**。只能给 `YYYY-MM-DD` |

分页固定每页 15 条（服务端 `lib.Paging(page, 15)`），没有 `pagesize` 参数。

#### `CoreSqlOrder.status` 取值

依据是上游真正写这个字段的代码路径，不是前端文案：

| 值 | 含义 | 依据 |
|---|---|---|
| `0` | 已驳回 | `RejectOrder()` 写死 `status = 0`，提示语「工单已驳回！」 |
| `1` | 已执行 | `ExecuteOrder()` 里 `type == 3` 的工单直接置 `1` |
| `2` | 审核中 | `ExecuteOrder()` 只接受 `2` 或 `5` 进入执行；`FetchUndo()` 只允许撤销 `2` |
| `3` | 执行中 | `Executor()` 交给执行器前先置 `3` |
| `4` | 执行失败 | `delayKill()` 置 `4`，提示语「状态已更改为执行失败！」 |
| `5` | 待执行 | 同上 —— `ExecuteOrder()` 接受 `5`，说明它还没执行过 |
| `7` | 全部 | `AccordingToAllOrderState()` 里 `case 7: return db`，7 不是数据库里的值 |

`status` 与 `query_per` 是两套编号：查询工单的 `1` / `2` / `3`
（已生效 / 待审核 / 已结束）见下面「查询」一节，别混看。

---

## 查询 `/api/v2/query/:tp`

| 方法 | tp | 说明 |
|---|---|---|
| POST | `refer` | 提交查询工单 —— **`ReferQueryOrder`**，写操作 |
| POST | `results` | **执行 SQL** —— **`QueryDeal`** |
| GET | `fetch_table` · `table_info` | 表结构（需要数据源参数，缺参数会报 `dial tcp :0: connection refused`） |
| PUT | `fetch_base` | 库信息 |
| PUT | `status` | 查询工单状态 |
| DELETE | （无 tp） | 结束自己的查询权限 —— 写操作 |

### 查询流程

Yearning 的查询不是"直接查"，而是三步：

```
POST /api/v2/query/refer    提交查询工单（申请某个环境的查询权限）
        ↓  管理员在网页上批准
PUT  /api/v2/query/status   状态变为 1 = 已生效，且有有效窗口
        ↓  窗口内
POST /api/v2/query/results  执行 SQL
```

窗口过期后状态变 `3`，需要重新提交查询工单。

### `POST /api/v2/query/refer` —— 提查询工单

请求体对应上游 `commom.QueryOrder`：

```json
{
  "idc":      "env-prod",
  "text":     "排查订单表数据异常，需要查询权限",
  "export":   0,
  "assigned": ""
}
```

`export` 非 `0` 表示允许把查询结果导出。`assigned` 一般留空，由流程配置决定审批人。

**同样没有服务端权限校验。** 只要 JWT 有效就能提交，`query_source` 的过滤只在
`fetch/source` 里。能提哪个环境，得自己判断。

### `POST /api/v2/query/results` —— 执行 SQL

请求体对应上游 `lib.QueryDeal`：

```json
{
  "source":    "db-prod",
  "data_base": "mydb",
  "sql":       "SELECT id, name FROM users LIMIT 10"
}
```

响应体（`payload`）：

```json
{
  "title":  [{"title": "id"}, {"title": "name"}],
  "data":   [{"id": 1, "name": "alice"}],
  "total":  1,
  "time":   12,
  "status": false
}
```

`status` 为真值时表示**查询权限已到期**，服务端没执行 SQL。

⚠️ **只校验「有没有生效中的查询工单」，不校验数据源属于哪个环境。**
批准了 A 环境，照样能拿它查 B 环境的库。调用方要自己核对
`query/status` 返回的 `idc` 与 `source` 的归属关系。

### `PUT /api/v2/query/status` —— 查询工单状态

返回 `{status, idc, export}`：

| `status` | 含义 |
|---|---|
| `1` | 已生效，可以执行查询 |
| `2` | 待审核，等管理员批准 |
| `3` | 已结束 / 已过期，需重新提交 |

⚠️ **这个接口会改状态但返回旧值。** 每次调用时它会检查窗口是否过期，
过期就顺手把状态改成 `3`，但**返回体给的是改动前的值**。
所以会出现「第一次调用显示 `1`、再调用变成 `3`」—— 那是窗口刚好在第一次调用时被判过期，
不是接口抽风。写客户端时别把第一次的结果缓存起来当长期事实用。

---

## 仪表盘 `/api/v2/dash/:tp`

| 方法 | tp | 说明 |
|---|---|---|
| GET | `count` | 系统统计，返回 `{createUser, order, query, source}`（用户数/工单数/查询数/数据源数） |
| GET | `pie` | 数据源查询量分布，返回 `[{data_base, count}, ...]` |

---

## 审核 `/api/v2/audit/*`

| 路径 | 说明 |
|---|---|
| `/api/v2/audit/order/:tp` | 工单审核 |
| `/api/v2/audit/osc/:work_id` | OSC 执行进度 |
| `/api/v2/audit/query/:tp` | 查询审核 |

中间件 `AuditGroup` 的逻辑是：`role == "guest"` 直接返回 **403 非法越权操作**。
这些接口都会改变工单状态 —— 本项目不提供任何调用入口。

---

## 管理 `/api/v2/manage/*`

| 路径 | 说明 |
|---|---|
| `/manage/group` GET | 我的权限组。**唯一对普通角色开放的 manage 接口**（源码里 `focalPoint()` 特意开了这个口子），返回 `{ddl_source, dml_source, auditor, query_source}` |
| `/manage/user` | 用户管理 |
| `/manage/db` | 数据源管理 |
| `/manage/board/post` | 发布公告 |
| `/manage/tpl` | 模板 |
| `/manage/setting` | 系统设置 |
| `/manage/roles` | 角色 |
| `/manage/task` | 自动任务 |

除 `/manage/group` 外，均需 super 权限。

---

## 权限矩阵

`/api/v2/manage/group` 返回的四个数组决定了一个账号能做什么：

| 字段 | 控制 |
|---|---|
| `query_source` | 能查哪些库的数据 |
| `ddl_source` | 能对哪些库提交 DDL 工单 |
| `dml_source` | 能对哪些库提交 DML 工单 |
| `auditor` | 能审哪些流程的工单 |

四个都为空时，账号只能读统计类接口（`dash/*`、`fetch/idc`、`fetch/board`），
查数据、提工单、审工单全部做不了。`fetch/source` 通常也会提示「环境没有添加流程」。

### 但别拿它当唯一依据

`manage/group` 与 `fetch/source` 走的是**两套不同的权限合并逻辑**，
实测中见过「`manage/group` 四个数组全空、`fetch/source` 却正常返回一串数据源」的情况。

**结论：判断真实提工单权限一律用 `fetch/source`**，`manage/group` 只当参考
（本项目据此逐环境实测 `fetch/source`）。

---

## 与 3.x 的差异（别混用）

| | 2.x | 3.x |
|---|---|---|
| 登录 | `POST /ldap` · `POST /login`（根路径） | `POST /api/v2/login` |
| 查询通道 | HTTP `/api/v2/query/results` | WebSocket + msgpack |
| 前端特征 | `/front/assets/js/chunk-*.js`（webpack） | `app.<hash>.js` |
| 迁移工具 | 无 | 跨版本升级用 `migrate`，不是 `install` |

---

## 三个平台侧安全问题（建议部署方自查）

1. **`GET /api/v2/fetch/perform` 以普通账号身份即可读取执行人的 PBKDF2 密码哈希。**
   响应里的 `perform` 数组每项都带 `password` 字段（形如 `pbkdf2_sha256$120000$...`）。
   虽然不可逆，但 12 万轮 PBKDF2 对弱口令可离线爆破，属未授权敏感信息泄露。
   建议在 `FetchPerformList` 的返回结构里去掉 `password`；如需保留，也应把该接口收进相应权限组。

2. **提工单接口（`SQLReferToOrder` / `ReferQueryOrder`）服务端零权限校验。**
   `ddl_source` / `dml_source` / `query_source` 的过滤只发生在 `fetch/source`。
   直接 POST 就能给任意数据源提工单、申请任意环境的查询权限。
   建议在这两个 handler 里补上与 `fetch/source` 一致的授权校验。

3. **`POST /api/v2/query/results` 不校验数据源归属环境。**
   只要 JWT 有效且有生效中的查询工单就能执行 SQL，批准了 A 环境可以查 B 环境的库。
   建议在 `QueryDeal` 里校验 `source` 是否属于该查询工单的 IDC。
