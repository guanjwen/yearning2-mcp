# yearning2-mcp

把 **Yearning 2.x**（SQL 审核平台）接入 MCP，让 Claude / Cursor / WorkBuddy 这类 AI 客户端
能直接查平台数据、**提工单**、**执行只读查询**。

- **零第三方依赖** —— 只用 Python 标准库。`pip install` 不会拖进 requests / pydantic 那一套
- **能力分三层，边界在服务端硬拦** —— 只读 / 提交 / 拦截，不靠提示词约束、也没有 `--force` 后门
- **不碰审批** —— 审批类接口一律拒绝。工单只提交，执行与否由人决定
- **提到线上之前先过闸门** —— 数据源授权、审核人合法、SQL 类型自洽、两步确认，四道都过才发请求
- **适配 2.x** —— 登录走根路径 `/ldap` · `/login`，业务接口在 `/api/v2/*`

---

## ⚠️ 只适用于 Yearning 2.x

如果你用的是 **Yearning 3.x**，本项目不适用 —— 3.x 的查询走 WebSocket + msgpack，
登录接口与静态资源打包方式都不一样，2.x 的接口形态在这上面跑不通。

怎么判断自己用的是哪个版本：打开 Yearning 首页看静态资源路径。形如
`/front/assets/js/chunk-*.js`（webpack 打包）的是 **2.x**；`app.<hash>.js` 那种打包风格的是 3.x。

---

## 快速开始

### 1. 安装

```bash
pip install yearning2-mcp        # 或
uvx yearning2-mcp                # 不安装，直接跑
```

### 2. 配置凭据

**推荐**：写一份配置文件，别把密码塞进客户端的配置里（那通常是明文、还会被同步到别处）。

```bash
mkdir -p ~/.yearning
cat > ~/.yearning/config.json <<'JSON'
{
  "endpoint": "http://yearning.example.com:8000",
  "username": "your-account",
  "password": "your-password",
  "login_mode": "ldap"
}
JSON
```

### 3. 接进 MCP 客户端

Claude Desktop 的 `claude_desktop_config.json`、Cursor 的 `mcp.json`、WorkBuddy 的
`~/.workbuddy/mcp.json` 都是同一个结构：

```json
{
  "mcpServers": {
    "yearning2": {
      "command": "yearning2-mcp"
    }
  }
}
```

用 `uvx` 的话把 `command` 换成 `"uvx"`、`args` 加 `["yearning2-mcp"]`。

不想写配置文件，也可以用环境变量（**优先级高于配置文件**）：

```json
{
  "mcpServers": {
    "yearning2": {
      "command": "yearning2-mcp",
      "env": {
        "YEARNING_ENDPOINT": "http://yearning.example.com:8000",
        "YEARNING_USERNAME": "your-account",
        "YEARNING_PASSWORD": "your-password"
      }
    }
  }
}
```

### 4. 先自检，再信任

```bash
yearning2-mcp --check-config     # 看解析到的配置对不对（不会打印密码）
yearning2-mcp --selftest         # 连通性 / 登录 / 权限 / 拦截规则，一次跑完
```

两个都过了，再回到 MCP 客户端里把这个 server **信任 / 启用**。多数客户端需要重启一次才加载新 server。

---

## 工具清单

14 个工具，按用途分三组。

### 只读（8 个，只发 GET）

| 工具 | 作用 |
|---|---|
| `yearning_status` | 连通性、登录方式、账号、角色、真实姓名、**各环境实测可提工单的数据源数量**。**排查问题先调这个** |
| `yearning_allowed_sources` | 某环境某类型下我实际可用的数据源清单 + 可选审核人。**提工单前必调** |
| `yearning_system_stats` | 用户数 / 工单总数 / 查询总数 / 数据源数 |
| `yearning_list_environments` | 环境(IDC)列表 |
| `yearning_datasource_usage` | 各数据源查询次数分布（前 30 个） |
| `yearning_board` | 首页公告板配置 |
| `yearning_orders` | 工单执行明细分页，可按环境 / 数据源 / 时间过滤 |
| `yearning_api_get` | 受控的只读 GET 透传，用于读上面没覆盖的只读接口 |

### 查询（3 个，走「先提工单、批准后查询」的官方流程）

| 工具 | 作用 | 需要确认 |
|---|---|---|
| `yearning_submit_query_order` | 申请某个环境的查询权限（进审核流程） | ✅ 两步 |
| `yearning_query_status` | 看查询工单状态：`1` 已生效 / `2` 待审核 / `3` 已结束 | — |
| `yearning_run_query` | 执行只读 SQL（`SELECT` / `SHOW` / `DESC` / `EXPLAIN`） | — |

### 工单（3 个）

| 工具 | 作用 | 需要确认 |
|---|---|---|
| `yearning_submit_order` | 提交 DDL / DML 工单（进审核流程，**不会立即执行**） | ✅ 两步 |
| `yearning_my_orders` | 我提交过的工单列表，可按状态 / 关键字 / 日期区间筛 | — |
| `yearning_revoke` | 撤销我自己的待审工单 / 结束我自己的查询权限 | ✅ 两步 |

接好之后不需要记工具名，直接说人话就行：

| 你说 | 会调 |
|---|---|
| 「Yearning 能连上吗」/「我有什么权限」 | `yearning_status` |
| 「我在 prod 能提哪些库的工单」 | `yearning_allowed_sources` |
| 「Yearning 上有多少工单和用户」 | `yearning_system_stats` |
| 「列一下 Yearning 有哪些环境」 | `yearning_list_environments` |
| 「哪个库被查得最多」 | `yearning_datasource_usage` |
| 「查工单，第 2 页每页 20 条」 | `yearning_orders` |
| 「把 user 表加个索引，提个工单」 | `yearning_submit_order`（先给预览，你点头才提交） |
| 「帮我查一下 xxx 库的订单表有多少行」 | `yearning_query_status` → `yearning_run_query` |
| 「我刚提的那个工单批了吗」 | `yearning_my_orders` |
| 「我今天的工单」 | `yearning_my_orders`（带 `since` / `until`） |
| 「我有哪些执行失败的工单」 | `yearning_my_orders`（`status="执行失败"`） |
| 「撤了刚才那个工单」 | `yearning_revoke` |

---

## 安全模型

拦截与闸门写在 `src/yearning2_mcp/safety.py` 与 `writes.py`，是**执行层**而不是提示词。
调用方（包括模型本身）绕不过去，除非改这两个文件 —— 属刻意行为，改完请同步改本文档与 `SECURITY.md`。

### 三层能力

| 层 | 入口 | 说明 |
|---|---|---|
| **只读 READ** | `yearning_api_get` 及 7 个只读工具 | 只发 GET。写操作型 GET（`fetch/undo`、`query/results`、`query/refer`）在这里被拒，防止从只读通道溜进写意图 |
| **提交 WRITE** | 6 个专用工具 | **不接受任意路径**：方法+路径写死在 `WRITE_ACTIONS` 白名单里，调用方只能提供业务参数 |
| **拦截 BLOCKED** | 无入口 | 审批类（`/api/v2/audit/**`）、管理类（`/api/v2/manage/**`，只放行只读的 `manage/group`）、泄露密码哈希的 `fetch/perform`、以及会绕过审核直接执行 SQL 的 `fetch/test` 等 |

### 四道闸门（提工单 / 执行查询时）

1. **数据源授权** —— 用 `GET /api/v2/fetch/source?idc=&tp=` 取真实授权清单，数据源必须命中。
   命中的是**清单里的原始字符串**（线上存在 `" db-readonly"` 这种带前导空格的脏记录，
   比对时 strip、发出去时原样）。
2. **审核人合法** —— `assigned` 必须来自服务端给的可选审核人清单。留空且清单非空时，直接把可选值列出来。
3. **SQL 与工单类型自洽** —— `ddl` 工单必须全是 DDL 语句，`dml` 工单必须全是 DML。
   `SELECT` 提工单会被拒（该走查询工单），DDL/DML 混写按 DML 归类。
4. **两步确认** —— 提工单、撤单、结束查询权限这四类动作**默认只返回预览**，
   要带 `confirm=true` 再调一次才真发请求。`yearning_run_query` 不在其中：
   它是只读且可重复执行的，每次都要求确认会让探索式查询没法用 —— 它的保护来自
   「SQL 只读性校验」+「数据源必须属于已批准查询工单的环境」这两条。

### 执行查询额外补的洞

`POST /api/v2/query/results` 只看「有没有生效中的查询工单」，**不校验数据源属于哪个环境**。
批准了 A 环境照样能查 B 环境的库。`yearning_run_query` 因此先读查询工单的环境，
再要求数据源落在该环境的 `query` 授权清单里。

### 只读 SQL 的三条判定规则

`writes.ensure_read_only()` 不靠大黑词表（那会把 `SELECT event, set_at FROM logs` 误杀），
而是三条精确规则：

1. 每条语句的**首词**必须是只读起始词（`SELECT` / `WITH` / `SHOW` / `DESC` / `DESCRIBE` / `EXPLAIN` / `TABLE` / `VALUES` / `HELP`）
2. `WITH` / `EXPLAIN` 这类复合前缀，整条语句里不得再出现写动词
3. 不得出现文件读写词（`OUTFILE` / `DUMPFILE` / `LOAD_FILE`）、危险函数（`SLEEP` / `BENCHMARK` / `GET_LOCK`）
   与锁语义（`FOR UPDATE` / `FOR SHARE` / `LOCK IN`）

判定前先做单趟状态机去噪声：注释、字符串字面量、反引号标识符里的内容都不参与判断 ——
否则 `SELECT '--' AS x` 会被当成注释截断。语句数上限 50 条。

### 边界强制在哪里

三层边界都在 `safety.py` / `writes.py` 的**执行层**强制，不是提示词约束：
拒绝判定发生在任何 HTTP 请求发出**之前**；写动作只能从 `WRITE_ACTIONS` 白名单出去，
方法 + 路径写死，调用方只能填业务参数、无法自定义目标路径。

---

## 配置参考

### 查找顺序（命中即停）

1. `--config` 指定的文件
2. 环境变量 `YEARNING_CONFIG` 指向的文件（两者都是显式指定：文件不存在会直接报错，不会静默回落）
3. `~/.yearning/config.json`
4. `./yearning.config.json`

### 环境变量（始终优先于文件）

| 变量 | 说明 |
|---|---|
| `YEARNING_ENDPOINT` | 地址，如 `http://yearning.example.com:8000`。缺协议时自动补 `http://` |
| `YEARNING_USERNAME` | 登录账号 |
| `YEARNING_PASSWORD` | 登录密码 |
| `YEARNING_LOGIN_MODE` | `ldap`（默认）或 `local` —— 决定先试 `/ldap` 还是 `/login` |
| `YEARNING_CONFIG` | 配置文件路径 |
| `YEARNING_SESSION` | 会话缓存路径，默认 `~/.yearning/session.json` |
| `YEARNING_MCP_LOG` | 日志路径，默认 `~/.yearning/mcp.log`；设 `off` 关闭 |

### 运行期文件

| 路径 | 内容 |
|---|---|
| `~/.yearning/session.json` | JWT + 真实姓名等会话缓存（POSIX 下权限 600）。按 endpoint 隔离，换实例不复用 |
| `~/.yearning/mcp.log` | 运行日志，排障看这个。超过 2MB 自动轮转 |

---

## 命令行

```bash
yearning2-mcp                    # 启动 stdio MCP 服务（MCP 客户端默认这么调）
yearning2-mcp --check-config     # 打印解析到的配置，不回显密码
yearning2-mcp --selftest         # 对真实实例自检：TCP / 登录 / 身份 / 权限 / 只读接口 / 拦截规则
yearning2-mcp --selftest --report report.txt
yearning2-mcp --print-tools      # 以 JSON 打印工具清单
yearning2-mcp --version
```

`--selftest` 的退出码：`0` 全部通过，`1` 连接或登录失败，`2` 配置有问题。

---

## 从源码跑

本项目用 `src/` 布局，所以要先装一次才能以模块方式启动：

```bash
git clone https://github.com/guanjwen/yearning2-mcp.git
cd yearning2-mcp

pip install -e .                # 或 pip install .
yearning2-mcp --print-tools     # 确认工具清单生成正常
yearning2-mcp --selftest        # 对真实实例自检
```

---

## 兼容性

| 项 | 情况 |
|---|---|
| Yearning | 2.3.x（路由表取自上游 `cookieY/Yearning` tag `2.3.5` 的 `src/router/router.go`，并在真实部署上逐条实测对齐） |
| 认证 | LDAP 已实测；本地账号走 `POST /login`，同一套逻辑 |
| 只读能力 | 全部接口已在真实部署上实测 |
| 提工单 | **已实测到预览与全部闸门**（含 4 条负向用例：越权数据源、非法审核人、类型不匹配、缺 confirm）。真实提交会真的产生线上工单，没有在别人的生产环境上做；提交与预览复用同一份请求体构造 |
| 执行查询 | 已实测闸门与拒绝路径；查询通道本身需要一条已批准的查询工单才能走通 |
| 审批 / 管理 | **不提供，也不打算提供** |
| Python | 3.9 ~ 3.14（3.13 / 3.14 已实测；CI 覆盖全矩阵） |
| 平台 | Windows / Linux / macOS（纯标准库，无平台相关代码） |
| 第三方依赖 | 无 |

---

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 工具报「无法连接」 | 内网地址，先连上 VPN。客户端会自动绕开系统代理（`HTTP_PROXY` 在这里是坑） |
| 工具报「登录失败」 | 账号密码错，或 LDAP 不可达。用 `--check-config` 确认凭据来源 |
| 返回字符串 `Illegal` | 子路径写错了。这是 Yearning 对错误子路径的兜底响应，**不是报错** |
| 报「环境没有添加流程」 | 该环境没配审批流程，`fetch/source` 直接返空 —— 提工单、查询都做不了，需要管理员配流程 |
| `yearning_allowed_sources` 返回空清单 | 你的账号在该环境该类型下没有授权。`yearning_status` 里的数量也会是 0 |
| `yearning_run_query` 报「没有生效中的查询工单」 | 先去 `yearning_submit_query_order` 申请，等管理员批准（`yearning_query_status` 看到 `1`） |
| 查询状态第一次显示已生效、再查变成已结束 | 上游行为：`query/status` 每次调用都会检查窗口是否过期，过期就顺手把状态改成 `3`，但返回体给的是**改动前的旧值**。不是工具抽风 |
| 提工单返回「预览」而不是「已提交」 | 这是设计如此。确认无误后带 `confirm=true` 再调一次 |
| MCP 客户端里看不到工具 | 确认配置是合法 JSON；多数客户端需要重启才加载新 server |
| 改了服务端代码不生效 | MCP server 是长驻子进程，不会热重载。改完要重启 MCP 客户端 |

---

## 平台侧的安全问题（非本项目缺陷）

在真实环境中实测时发现的三个问题，**都属于 Yearning 服务端**，本项目只能在自己这一侧补闸门。
注意：**绕过本 MCP 直接调这些接口，一样能命中。**

1. **`GET /api/v2/fetch/perform` 以普通账号身份即可调用，响应体里带着执行人的 PBKDF2 密码哈希。**
   哈希不可逆，但 12 万轮 PBKDF2 对弱口令可以离线爆破，属未授权敏感信息泄露。
   建议在 `FetchPerformList` 的返回结构里去掉 `password` 字段，或把该接口收进相应权限组。
   本项目已主动把该接口拦掉，不让它经 MCP 进入对话记录。

2. **提工单接口（`SQLReferToOrder` / `ReferQueryOrder`）在服务端一行权限校验都没有。**
   `ddl_source` / `dml_source` 的过滤只发生在 `GET /api/v2/fetch/source` 里 ——
   也就是前端下拉框。直接 `POST /api/v2/common/order` 可以给任意数据源提工单。
   **本项目补上了这道闸门**（闸门 1），但服务端该补的还是要补。

3. **`/api/v2/query/results` 只看「有没有生效中的查询工单」，不校验数据源属于哪个环境。**
   批准了 A 环境，就能拿它查 B 环境的库。**本项目在 `yearning_run_query` 里补了环境归属校验**，
   同样，服务端该补的还是要补。

---

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。核心约定两条：**保持零依赖**、
**写操作必须走白名单 + 闸门，且不引入任何审批类能力**。

## 许可

[MIT](LICENSE)。

本项目是独立实现的 HTTP 客户端，**不包含** Yearning 的任何代码。
上游 [cookieY/Yearning](https://github.com/cookieY/Yearning) 采用 AGPL-3.0，两者互不影响。

## 致谢

- [cookieY/Yearning](https://github.com/cookieY/Yearning) —— 接口形态与路由规则来自其 2.3.5 版源码
- [Model Context Protocol](https://modelcontextprotocol.io/) —— 协议规范
