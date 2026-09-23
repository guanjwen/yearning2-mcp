# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)，
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.2.0] - 2026-09-23

把 Yearning 2.x 接成 MCP：只读查询 + 提工单 + 执行查询，**不碰审批**。

### 新增

- 14 个工具，按用途分三组：只读 8 个 / 查询 3 个 / 工单 3 个
- **新模块 `writes.py`**：纯逻辑、不发请求，可脱离网络单独验证
  - 只读 SQL 用三条精确规则判定（起始词 / 复合前缀不夹写动词 / 无文件读写与锁语义），
    不用黑词表 —— 黑词表会把 `SELECT event, set_at FROM logs` 这类正常查询误杀
  - 单趟状态机去噪声：注释、字符串字面量、反引号标识符不参与判定，
    避免 `SELECT '--' AS x` 被当成注释截断
  - `check_order_type()`：SQL 与工单类型自洽校验，DDL/DML 混写按 DML 归类，`SELECT` 提工单拒收
  - `resolve_source()`：比对授权清单时 strip，但返回**清单里的原始字符串** ——
    线上存在 `" db-readonly"` 这种带前导空格的脏记录，Yearning 执行时是 `WHERE source = ?` 精确匹配
  - `order_payload()` / `query_order_payload()` / `query_body()`：请求体构造
- **四道闸门**（提工单 / 执行查询）：
  1. 数据源必须命中 `fetch/source` 给出的真实授权清单
  2. 审核人 `assigned` 必须来自服务端的可选清单
  3. SQL 与工单类型自洽
  4. 提工单 / 撤单 / 结束查询权限默认只返回预览，需显式 `confirm=true` 才发请求
- **补掉两个上游服务端缺失的校验**：
  - `SQLReferToOrder` / `ReferQueryOrder` 服务端零权限校验，本 MCP 用闸门 1 补上
  - `POST /api/v2/query/results` 不校验数据源归属环境（批准 A 环境能查 B 环境），
    `yearning_run_query` 补上环境归属校验
- **`safety.py` 的三层模型**：只读 READ / 提交 WRITE / 拦截 BLOCKED
  - `GET_BLOCKED_EXACT`：`fetch/undo`、`query/results`、`query/refer` 这三个
    「路径看着像只读、实际是写操作」的 GET 一律拒绝，引导到对应专用工具
  - `WRITE_ACTIONS` 白名单：方法 + 路径写死，调用方只能填业务参数
  - `check_write()` / `describe_action()` / `needs_confirm()` / `write_summary()`
- **本地校验全部先跑完再碰网络**：`yearning_submit_order` 的参数、类型、SQL 校验
  都在任何请求发出之前完成 —— 参数写错不该先打一次线上
- **中文宽度对齐**：预览输出用 `unicodedata.east_asian_width` 按**显示宽度**补齐，
  修掉 `%-9s` 对中文按字符数补齐导致的错位
- **`yearning_status` 实测真实权限**：逐环境调 `fetch/source` 统计可提工单的数据源数量
- **工具分工**：`yearning_orders` 用于按 work_id 读工单执行明细，
  「我提交过的工单列表」由 `yearning_my_orders` 提供

### 已知限制

- 仅适配 2.x。3.x 的查询走 WebSocket + msgpack，接口形态不同，不适用
- **不提供审批能力，也不打算提供。** 审批类接口（`/api/v2/audit/**`）一律拒绝
- 管理类接口（`/api/v2/manage/**`）除只读的 `manage/group` 外全部拒绝
- 提工单的**真实提交**未在他人生产环境上实测（会真的产生线上工单）；
  已实测到预览与四道闸门的全部拒绝路径，提交与预览复用同一份请求体构造
