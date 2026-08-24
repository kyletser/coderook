# MCP 互操作合同

CodeRook 是 MCP client，不把任意第三方 server 的行为纳入自身信任边界。当前支持 stdio、历史 TCP、
legacy SSE 和 Streamable HTTP；正式互操作矩阵使用官方 Python SDK `mcp[cli]==2.0.0` 的真实 server，
不使用只会回固定 JSON 的自制 mock 代替外部协议证据。

## 支持矩阵

| Transport | 连接方式 | 安全边界 | 官方 fixture |
|---|---|---|---|
| stdio | daemon 监督的子进程 stdin/stdout | 继承声明的进程权限，stderr 独立排空 | 是 |
| legacy SSE | 长连接 GET `/sse` + server 指定的 POST endpoint | 远端必须 HTTPS；POST endpoint 必须同源 | 是 |
| Streamable HTTP | 单一 POST/GET endpoint、session id、SSE/JSON response | 远端必须 HTTPS；支持 GET cursor 重连 | 是 |
| TCP | loopback/raw NDJSON 兼容入口 | 不作为公开远端协议；没有官方 SDK 矩阵 | 否 |

互操作 runner 对每个官方 transport 验证：initialize/capabilities、tools list/call、resources list/read、
prompts list/get、`notifications/cancelled` 确实取消 slow tool，以及关闭首连接后重新握手并发现工具。
Streamable HTTP 的 `Last-Event-ID` 有界重连另由确定性单测覆盖，因为官方 fixture 不主动发布可恢复事件。

## 复现

运行器通过 `uvx` 启动固定版本官方 SDK，不修改项目运行时依赖：

```bash
uv run python scripts/run_mcp_official_interop.py \
  --output-dir .interop-results/mcp
```

输出 `mcp-official-interop.json` 和 Markdown 矩阵。该命令会从包索引下载固定 SDK；普通 `pytest` 不会
联网或运行它。GitHub 的 `mcp-interop.yml` 仅在手动触发时保存相同 artifact，避免日常 PR 运行重型兼容矩阵。

当前 checked-in 证据为 [SDK 2.0.0 / Windows / commit c47ae23](../evidence/mcp-official-sdk-2.0.0/mcp-official-interop.md)，
三种 transport 的 tools/resources/prompts/cancellation/reconnect 均通过。对应 JSON 保留逐项布尔值和
完整 commit，由公开仓库检查器验证，不把 Markdown 总结当唯一证据。

仓库中的 dated 报告只证明报告所列 commit、平台和 SDK 版本；SDK 升级必须先修改 pin，审查协议差异，
再生成新报告。一次 fixture 通过不代表所有 server、OAuth provider、sampling、elicitation 或扩展都兼容。

## 失败与降级

- initialize、HTTP status、同源校验、匹配 request id 或 30 秒响应超时失败会抛
  `McpServerUnavailableError`，不会把缺失响应当空成功；
- JSON-RPC application error 使用 `McpToolError`，工具层把它转换为可见失败；
- 本地调用被取消时发送标准 `notifications/cancelled`；server 不响应时本地任务仍结束并记录失败；
- legacy SSE endpoint 不能把 POST 地址重定向到不同 scheme/host/port；远端明文 HTTP 在发送凭据前拒绝；
- OAuth、动态客户端注册和企业代理互操作仍不是当前完成项，边界见 `docs/reference/FUNCTIONAL_ARCHITECTURE.md`。
