# 贡献指南

先谢谢你有兴趣。这个项目很小，约定也很少，但下面两条是硬的。

## 两条硬约定

1. **保持零第三方依赖。** 只用 Python 标准库。想加依赖之前先想清楚 —— 一个 MCP server
   的部署便利性就来自这里。`pyproject.toml` 里的 `dependencies` 必须保持为空。
2. **写操作只有 7 个，且必须走白名单 + 闸门。** 新增写能力时：
   - 先在 `safety.WRITE_ACTIONS` 加一条 `动作名 → (方法, 路径)`，路径写死，**不接受调用方传路径**
   - 纯逻辑校验放 `writes.py`，HTTP 编排放 `tools.py`
   - 需要确认的动作用 `safety.needs_confirm` + 显式 `confirm` 参数，默认只返回预览
   - **本地可判定的校验必须排在网络请求之前**，否则参数写错也会先在线上留痕迹
   - 仍然**不引入任何审批能力** —— 审批应该在 Yearning 网页上人工确认后执行

改拦截规则或闸门时，**同步改 `README.md` 的安全模型表格**。

改只读判定（`writes.ensure_read_only`）时请手工过一遍这类语句：
`SELECT event, set_at, load_user FROM logs` 带三个看起来"敏感"的词，但它必须被放行；
`SELECT 1; DROP TABLE t` 必须被拒。**只测「危险的被拒」不够，还得测「正常的没被误杀」** ——
大黑词表把正常查询拒掉就是这么来的。

## 开发环境

不需要装任何东西：

```bash
git clone https://github.com/guanjwen/yearning2-mcp.git
cd yearning2-mcp
python -m yearning2_mcp --print-tools    # 确认工具清单能正常生成
```

跑真实环境自检（**别把真实地址和凭据写进仓库**）：

```bash
export YEARNING_ENDPOINT=http://yearning.example.com:8000
export YEARNING_USERNAME=someone
export YEARNING_PASSWORD=******
python -m yearning2_mcp --selftest
```

`--selftest` 会真的发请求（TCP / 登录 / 身份 / 权限 / 只读接口 / 拦截规则），
所以它需要一个能连上的实例，别指望在离线环境里跑。

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
2. 提交信息用中文，格式：`类型: 简短说明`（类型用 `feat` / `fix` / `docs` / `chore`）
3. PR 描述里写清楚：改了什么、为什么、**怎么验证的**

如果是新增一个 Yearning 接口的适配，请在 PR 里说明你是怎么确认这个接口形态的
（源码路径 / 实测请求响应），别只贴结论。

## 发布的约定（维护者）

1. 更新 `src/yearning2_mcp/__init__.py` 的 `__version__`
2. 更新 `CHANGELOG.md`
3. 打 tag：`git tag -a v0.2.1 -m "v0.2.1"`
4. `python -m build`
5. `twine check dist/*`
6. `twine upload dist/*`
