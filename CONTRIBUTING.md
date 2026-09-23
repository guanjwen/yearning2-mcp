# 贡献指南

先谢谢你有兴趣。这个项目很小，约定也很少，但下面四条是硬的。

## 四条硬约定

1. **保持零第三方依赖。** 只用 Python 标准库。想加依赖之前先想清楚 —— 一个 MCP server
   的部署便利性就来自这里。`pyproject.toml` 里的 `dependencies` 必须保持为空。
2. **写操作只有 7 个，且必须走白名单 + 闸门。** 新增写能力时：
   - 先在 `safety.WRITE_ACTIONS` 加一条 `动作名 → (方法, 路径)`，路径写死，**不接受调用方传路径**
   - 纯逻辑校验放 `writes.py`（离线可测），HTTP 编排放 `tools.py`
   - 需要确认的动作用 `safety.needs_confirm` + 显式 `confirm` 参数，默认只返回预览
   - **本地可判定的校验必须排在网络请求之前**，否则参数写错也会先在线上留痕迹
   - 仍然**不引入任何审批能力** —— 审批应该在 Yearning 网页上人工确认后执行
3. **新增拦截规则必须同时改三处**：`src/yearning2_mcp/safety.py`、`tests/test_safety.py`
   （正例与反例都要）、以及 `README.md` 的安全模型表格。改 `writes.py` 的只读判定时，
   `tests/test_writes.py` 的 `ALLOWED` / `DENIED` 两张表要同时补 —— 只测「危险的被拒」
   不够，**必须同时测「正常的没被误杀」**（黑词表把 `SELECT event FROM logs` 拒掉就是这么来的）。
4. **负向用例要断言"没发出请求"。** 光断言抛异常不够 —— 那样改改提示语就能蒙过测试。
   用 `fake.write_bodies(path)` 断言对应接口一次都没被调用。

## 开发环境

不需要装任何东西：

```bash
git clone https://github.com/guanjwen/yearning2-mcp.git
cd yearning2-mcp
python run_tests.py -v
```

跑真实环境自检（**别把真实地址和凭据写进仓库**）：

```bash
export YEARNING_ENDPOINT=http://yearning.example.com:8000
export YEARNING_USERNAME=someone
export YEARNING_PASSWORD=******
python -m yearning2_mcp --selftest
```

## 测试

测试全部离线，靠 `tests/fake_yearning.py` 里的假服务。它刻意复刻了真实服务的怪癖
（错误子路径返回 `"Illegal"`、登录在根路径、`fetch/perform` 带密码哈希、
`fetch/source` 空参数返回空响应体、数据源名带前导空格），
所以「测试过了线上却挂」这类问题基本能提前挡住。

新增接口或工具时请一并补：

- 假服务里的对应路由
- 工具输出断言
- 如果是敏感或高危路径，补一条「被拒绝**且真的没有发出去**」的断言
  （参考 `test_api_get_refuses_dangerous_paths_without_calling_them`）

## 写代码时注意

- **别用 `print`。** stdio 模式下 stdout 就是协议通道，一句 `print` 会直接搞死连接。
  要记东西用 `yearning2_mcp._log.log()`，它写文件。服务端启动时会把 `sys.stdout`
  换成落盘陷阱兜底，但别指望它。
- **异常别往外抛到主循环。** 工具层抛 `ToolError`（会转成 `isError: true` 的返回），
  其余由 `McpServer.handle` 兜底。
- 代码风格：4 空格缩进、单引号、`%` 格式化（为了 3.9 兼容，别用 f-string 之外的新语法糖），
  行宽别超过 90。见 `.editorconfig`。
- 注释和文档用中文；面向代码的标识符用英文。

## 提 PR

1. 从 `main` 切分支
2. 改动 + 测试，`python run_tests.py` 全绿
3. 提交信息用中文，格式：`类型: 简短说明`（类型用 `feat` / `fix` / `docs` / `test` / `chore`）
4. PR 描述里写清楚：改了什么、为什么、怎么验证的

如果是新增一个 Yearning 接口的适配，请在 PR 里说明你是怎么确认这个接口形态的
（源码路径 / 实测请求响应），别只贴结论。

## 发布的约定（维护者）

1. 更新 `src/yearning2_mcp/__init__.py` 的 `__version__`
2. 更新 `CHANGELOG.md`
3. 打 tag：`git tag -a v0.2.0 -m "v0.2.0"`
4. `python -m build`
5. `twine check dist/*`
6. `twine upload dist/*`
