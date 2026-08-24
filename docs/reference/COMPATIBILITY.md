# 外部接口兼容与弃用策略

本文冻结 CodeRook 供第三方脚本、IDE 和 SDK 使用的接口边界。它不把 Python 内部模块、TUI
控件或 SQLite 私有表结构承诺为公共 API。

## 版本面

| 接口 | 当前版本 | 版本位置 | 协商方式 |
|---|---:|---|---|
| HTTP/JSON | `v1` | URL 前缀 `/v1/` | `GET /v1/capabilities` 与 `X-CodeRook-API-Version` 响应头 |
| SSE | durable event schema `1` | `data.schema_version` | capabilities 的 `runtime_event_schema_version` |
| `stream-json` / 单次 `json` | schema `1` | 每行或结果的 `schema_version` | capabilities 的 `stream_json_schema_versions` |
| Python SDK | 与 CodeRook 包同版本 | `code_rook.__version__` | SDK 的 `capabilities()` |
| IPC/NDJSON | 由类型模型生成 | `WIRE_PROTOCOL.md` | `runtime.capabilities`；主要供同版本 Core 与内置客户端使用 |

HTTP/SSE 与 headless 格式是公共机器接口。内部 IPC 当前只保证同一 CodeRook 版本的 Core、TUI
和 CLI 互通，不承诺任意跨版本组合。

Capabilities 还返回三级 feature flags：`stable` 是当前兼容合同，`labs` 表示功能存在但 UX/恢复语义
仍可在 v1 前变化，`internal` 不承诺给第三方调用。当前 durable session/turn、Provider Catalog、
Checkpoint、Change Center、有界 Goal、基础子 Agent、Skills、MCP Tools 和 Memory 位于 stable；
Fleet、declarative Workflow、Hooks v2、MCP Resources/Prompts 与 VS Code 原型位于 Labs。
调用方不得仅因包中存在命令或模块就把 Labs 当作稳定接口。Capabilities 的 `labs_enabled` 另行表示
当前进程是否由 `CODEROOK_LABS=1` 显式激活实验控制面；默认 false。即使为 true，Labs 仍不获得 v1
兼容承诺。

## v1 中允许的兼容变更

- 增加 endpoint、可选请求字段、响应字段、capability 或 feature；
- 增加 SSE 事件类型，或在事件 `payload` 中增加字段；
- 增加 `stream-json` 的领域事件类型，或在 `payload` 中增加字段；
- Python SDK 增加方法或为关键字参数提供向后兼容默认值。

调用方应忽略未知的 JSON 字段、SSE 事件类型和 capability。SSE record 的顶层字段
`thread_id / turn_id / seq / type / payload / ts / schema_version` 在 schema 1 内保持稳定；
headless envelope 的顶层字段在 schema 1 内同样保持稳定。

以下属于破坏性变化，必须使用新的 `/v2/` 路径或 `schema_version: 2`：删除或改名已有字段、
改变字段类型、增加必填请求字段、改变 durable `seq` 的排序语义、把成功状态改为错误状态，或改变
既有 headless 顶层 envelope 的含义。

## 错误与退出码

- HTTP v1 错误响应为 JSON `{"error":"message"}`。调用方可以依赖 HTTP 状态码，不应匹配
  message 文案；`400/401/404/409/500` 分别表示请求、鉴权、资源、状态冲突与内部错误类别。
- Python SDK 将非 2xx 响应映射为 `SdkError`，稳定公开 `status_code`；异常字符串只用于诊断。
- `coderook run` 成功返回 `0`，普通运行失败返回 `1`，需要人工审批且 headless fail-fast 时返回
  `3`，用户中断返回 `130`。stdout 在 `json`/`stream-json` 模式只承载协议，诊断写 stderr。

## SSE 重连语义

事件 `seq` 在单个 thread 内 durable 且单调递增。`after_seq` query 优先于
`Last-Event-ID` header，服务只发送严格大于游标的已提交事件。客户端应在处理成功后持久化最后
一个 `seq`，重连时传回，并按 `seq` 去重；不要使用到达时间推断顺序。15 秒 keepalive 注释不是
领域事件，也不推进游标。

## 弃用窗口

公共 v1 endpoint、字段、SDK 方法或 schema 1 在宣布替代方案后，至少保留 **两个已发布 minor
版本且不少于 90 天**，以两者中较晚者为准。弃用必须同时：

1. 写入 `CHANGELOG.md` 和本文；
2. 给出替代接口与迁移示例；
3. HTTP 接口在窗口内返回标准 `Deprecation` 和 `Sunset` header；SDK 在调用旧方法时发出
   `DeprecationWarning`；
4. 兼容 fixture 在窗口结束前保留在测试中。

安全漏洞可能要求更快地关闭危险行为。此时安全公告必须解释影响、最短安全迁移路径和缩短窗口的
理由；不能以安全名义静默改变无关协议。

## 消费者升级顺序

先请求 `capabilities()` 并确认 `api_version`；再按 feature 决定是否调用可选能力。升级 CodeRook
后先运行现有集成测试，再更新 SDK。不要根据包版本猜测 endpoint，也不要读取 runtime.db 私有表来
绕过公共 API。
