# -*- coding: utf-8 -*-
"""yearning2-mcp —— 把 Yearning 2.x 接成 MCP 的服务端。

目标是适配 **Yearning 2.x**（登录在根路径 /ldap 与 /login，业务接口在 /api/v2/*）。
3.x 的接口形态不同（登录带 /api 前缀、查询走 WebSocket + msgpack），不适用。

能力分三层，详见 :mod:`yearning2_mcp.safety`：

* 只读 —— 统计、环境、数据源、公告板、工单明细
* 提交 —— 提 DDL/DML 工单、提查询工单、执行只读查询、撤销自己的工单
* 拦截 —— 审批类、管理类、泄露密码哈希的接口，以及绕过审核直接改数据的执行类
"""

__version__ = '0.2.0'
__all__ = ['__version__']
