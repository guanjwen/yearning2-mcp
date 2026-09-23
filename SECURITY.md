# 安全策略

## 支持范围

只维护最新发布版本的安全问题。发现漏洞请按下面的方式上报。

## 上报漏洞

请使用 GitHub 的 **Security Advisory**（仓库页 `Security` → `Report a vulnerability`）私密上报，
不要开公开 issue。

请在报告里说明：

- 影响版本
- 复现步骤（最小可复现即可）
- 影响：能读到什么 / 能做到什么
- 如果有，给出你建议的修法

**请不要在报告里附带真实环境的地址、账号、密码或业务数据。** 用占位符描述即可。

## 本项目的安全模型

`yearning2-mcp` 的定位是**带闸门的接入层**：只读、提交（不提审批）、拦截，三层边界都由
`src/yearning2_mcp/safety.py` 与 `writes.py` 在**执行层**强制，不是提示词约束。

### 1. 写能力只从专用工具出去，路径写死

`yearning_api_get` 只放行 GET，且对写操作型 GET（`fetch/undo`、`query/results`、`query/refer`）
也一律拒绝 —— 否则「只读通道」会变成写意图的旁路。

真正的写动作走 `WRITE_ACTIONS` 白名单，**方法 + 路径在代码里写死**，调用方只能填业务参数，
无法自定义目标路径。白名单只有 7 条，且不含任何 `/api/v2/audit/**` 或 `/api/v2/manage/**`。

由 `tests/test_safety.py::test_paths_are_hardcoded` 与
`test_no_audit_or_manage_action_exposed` 守着。

### 2. 拒绝清单在归一化后的路径上判定

`%2e%2e`、尾部斜杠、大小写混杂、带查询串这些变体会先被 `normalize_path()` 归一化再进判定，
避免编码绕过。`tests/test_safety.py::test_normalize_strips_query_and_case` 覆盖。

### 3. 四道闸门（提工单 / 执行查询）

| 闸门 | 做什么 | 为什么必须由本 MCP 做 |
|---|---|---|
| 数据源授权 | `fetch/source?idc=&tp=` 取真实清单，命中才继续 | 上游提工单接口**没有任何权限校验**，直接 POST 能给任意库提工单 |
| 审核人合法 | `assigned` 必须来自服务端的可选清单 | 防止把工单派给不存在或不该收到的人 |
| SQL/类型自洽 | `ddl` 工单必须全是 DDL，`dml` 必须全是 DML，`SELECT` 拒收 | 防止拿 DDL 工单夹带 DML 绕过审核人的预期 |
| 两步确认 | 提工单/撤单/结束查询**默认只返回预览**，需显式 `confirm=true` | 让"模型自己决定就提交了"变成不可能 |

`yearning_run_query` 不要求两步确认（只读且可重复，每次确认会让探索式查询没法用），
它的保护换成另外两条：**只读 SQL 校验** + **数据源必须属于已批准查询工单的环境**。

### 4. 只读 SQL 用规则判定，不用黑词表

黑词表很容易误杀正常查询（`SELECT event, set_at, load_user FROM logs` 就带三个"敏感词"），
这里改用三条精确规则（见 `README.md` 的「只读 SQL 的三条判定规则」）。
判定前先做单趟状态机去噪声 —— 注释、字符串字面量、反引号标识符里的内容不参与判断，
否则 `SELECT '--' AS x` 会被当成注释截断，后面的内容就成了规则盲区。

### 5. 不把敏感字段往对话里带

`/api/v2/fetch/perform` 明确拦截 —— 它的响应体含执行人的 PBKDF2 密码哈希，
没有任何正当理由让它出现在 AI 对话记录里。

### 6. 凭据不进客户端配置

推荐把密码放在 `~/.yearning/config.json`（权限 600），而不是写在 MCP 客户端的配置文件里
（那里通常是明文、还可能被同步到别处）。`--check-config` 打印配置时也不会回显密码。

### 7. 会话缓存按实例隔离

`~/.yearning/session.json` 记了 endpoint，换了 Yearning 实例不会复用旧 token；
POSIX 下写入后 `chmod 600`。

### 8. stdout 是协议通道

stdio 模式下任何 `print` 都会污染 JSON-RPC 流。服务端启动时把 `sys.stdout` 换成落盘陷阱兜底，
但开发时请用 `yearning2_mcp._log.log()`。

### 没有后门

不存在 `--force` / `--unsafe` / 环境变量开关之类的绕过方式。
**要放开只能改源码** —— 属刻意行为，改完请同步改 `README.md` 与本文档。

## 使用方的责任

- **按最小权限授权。** 给这个 MCP 用的 Yearning 账号，权限组能小就小。
  只读统计类需求根本不需要 `query_source`。
- **知道闸门只护住本 MCP 这一侧。** 绕过它直接调 Yearning 接口，服务端该有的洞还在。
  它能降低"AI 误操作"的概率，不是 Yearning 的安全加固层。
- **不要用超级管理员账号。** 没有理由。
- **提交出去的东西是真会进审核队列的。** 虽然两步确认能挡住手滑，
  但确认之后的工单就是真工单 —— 审核人看到的东西影响他的判断，`text` 请写清楚。
- **日志与缓存文件当凭据看待。** `~/.yearning/` 下的文件含 JWT，别提交、别外发。

## 已知的平台侧问题（非本项目缺陷）

在真实部署上实测时发现的三个问题，属于 Yearning 服务端层面，本项目只能在自己这一侧规避。
部署方建议自查：

| 问题 | 影响 | 建议 |
|---|---|---|
| `GET /api/v2/fetch/perform` 对普通账号开放，响应体含执行人的 PBKDF2 密码哈希（`pbkdf2_sha256$120000$...`） | 未授权敏感信息泄露；弱口令可离线爆破 | 在 `FetchPerformList` 返回结构里去掉 `password` 字段，或把该接口收进相应权限组 |
| `SQLReferToOrder`（提 DDL/DML 工单）与 `ReferQueryOrder`（提查询工单）**服务端零权限校验**；`ddl_source`/`dml_source` 的过滤只发生在 `fetch/source`，即前端下拉框 | 直接 POST 即可给任意数据源提工单，绕过授权 | 在 `SQLReferToOrder` / `ReferQueryOrder` 里补上与 `fetch/source` 一致的授权校验 |
| `/api/v2/query/results` 只要 JWT 有效且有生效中的查询工单即可执行 SQL，**不校验数据源属于哪个环境** | 批准 A 环境可查 B 环境的库 | 在 `QueryDeal` 里校验 `source` 归属该查询工单的 IDC |

本项目的对应处理：

- `fetch/perform` 加入拦截名单，不提供任何调用入口
- 提工单前用 `fetch/source` 复核授权清单，不在清单内直接拒绝（闸门 1）
- `yearning_run_query` 先读查询工单的环境，再要求数据源落在该环境的 `query` 清单里
- 会绕过审核直接执行 SQL 的 `fetch/test`、`fetch/merge`、`fetch/marge`、`fetch/roll_order`
  全部硬拦截，不提供入口

**再次强调**：以上都是本 MCP 自己这一侧的补偿，不是服务端修复。
