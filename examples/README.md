# 示例配置

## `config.example.json`

复制到 `~/.yearning/config.json` 后改掉三个值即可：

```bash
mkdir -p ~/.yearning
cp examples/config.example.json ~/.yearning/config.json
```

| 字段 | 说明 |
|---|---|
| `endpoint` | Yearning 地址。缺协议时自动补 `http://` |
| `username` | 登录账号 |
| `password` | 登录密码 |
| `login_mode` | `ldap`（默认，先试 `POST /ldap`）或 `local`（先试 `POST /login`） |

这个文件含明文密码，注意权限（POSIX 下 `chmod 600`），别提交进 git。
`.gitignore` 已排除 `.yearning/` 和 `config.json`，即使把它们放在仓库目录里也不会被误提交。

## `mcp-servers.example.json`

是一个最小的 MCP 客户端配置片段，粘贴到你所用客户端的配置里：

| 客户端 | 配置文件 |
|---|---|
| Claude Desktop | `claude_desktop_config.json` |
| Cursor | `mcp.json` |
| WorkBuddy | `~/.workbuddy/mcp.json` |

三种接入方式，挑一种：

**A. 已经 `pip install yearning2-mcp`** —— 直接就是 `mcp-servers.example.json` 的内容。

**B. 不想安装，用 uvx**：

```json
{
  "mcpServers": {
    "yearning2": {
      "command": "uvx",
      "args": ["yearning2-mcp"]
    }
  }
}
```

**C. 从源码跑**：

```json
{
  "mcpServers": {
    "yearning2": {
      "command": "python",
      "args": ["C:/path/to/yearning2-mcp/src/yearning2_mcp/__main__.py"]
    }
  }
}
```

改完配置后**重启客户端** —— MCP server 是长驻子进程，不会热重载。
