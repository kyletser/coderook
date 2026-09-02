# Runtime API v1

CodeRook Core 在统一的 durable Thread/Turn/Item/Event runtime 上提供本地 HTTP/JSON 与
SSE 接口。默认监听 `127.0.0.1:7438`；TUI、IPC 和 HTTP 读取同一个
`~/.coderook/runtime.db`，不会分别维护近似状态。

版本协商、允许的增量变化、错误稳定边界和弃用窗口见
[《外部接口兼容与弃用策略》](COMPATIBILITY.md)。所有 JSON 与 SSE 响应都带
`X-CodeRook-API-Version: v1`；调用方仍应以 `/v1/capabilities` 的结果做 feature 协商。

## 安全绑定

- Runtime API 始终使用 Bearer token，包括 loopback 请求。
- 非空 `CODEROOK_API_TOKEN` 优先；空或纯空白值视为未配置，不能关闭鉴权。未配置时 Core 以
  no-follow/排他创建语义加载或创建 `~/.coderook/api-token`。POSIX 要求当前用户所有且严格为
  `0600`；Windows 不虚假承诺 POSIX mode，而是验证父目录、普通文件、重解析点和句柄/路径身份边界。
- 外部集成的每个 HTTP/JSON 和 SSE 请求都必须发送 `Authorization: Bearer <token>`。
- token 不进入项目配置、日志、事件或 Turn Receipt。

CodeRook Web 不读取 Bearer token。`coderook web` 经已认证 IPC 获取固定 loopback URL；浏览器首次调用
`GET /v1/web/session` 时自动获得 HttpOnly、SameSite=Strict Cookie 与内存 CSRF token。浏览器写请求
必须带 `X-CodeRook-CSRF`，静态资源与 Web API 只接受当前 loopback Host。旧版
`POST /v1/web/bootstrap` 保留兼容但不再要求有效的一次性票据；Provider API Key 不进入 URL 或浏览器存储。

```powershell
$env:CODEROOK_API_HOST = "127.0.0.1"
$env:CODEROOK_API_PORT = "7438"
uv run coderook-core
```

## JSON 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/v1/projects` | 列出用户登记的项目、当前活动工作区和默认空白项目目录 |
| `POST` | `/v1/projects` | 创建空白项目目录，body: `{"name":"...","parent":"..."}`；parent 可省略 |
| `POST` | `/v1/projects/open` | 登记电脑上已有的目录，body: `{"path":"..."}`；不复制文件 |
| `POST` | `/v1/projects/activate` | 在无活动 run 时原地切换单一活动工作区；浏览器认证与监听端口保持不变 |
| `DELETE` | `/v1/projects` | 忘记非活动项目记录，不删除磁盘文件 |
| `GET` | `/v1/filesystem/directories` | 为本机 Web 目录选择器列出驱动器或直接子目录 |
| `GET` | `/v1/threads` | 列出 durable threads；摘要包含 `turn_count`，供客户端优先恢复非空会话 |
| `POST` | `/v1/threads` | 创建 thread，body: `{"title":"...","mode":"chat"}` |
| `GET` | `/v1/threads/{id}` | 读取单个 durable thread |
| `PATCH` | `/v1/threads/{id}` | 更新标题或归档，body: `{"title":"...","archived":true}` |
| `DELETE` | `/v1/threads/{id}` | 显式确认后删除空闲 thread |
| `POST` | `/v1/threads/{id}/fork` | 创建独立会话 fork |
| `GET` | `/v1/threads/{id}/export` | 导出 markdown/json 正文 |
| `GET` | `/v1/threads/{id}/context` | 上下文与 checkpoint 摘要 |
| `GET` | `/v1/threads/{id}/turns` | 列出 durable turns；支持 `limit=1..100` 与 `before={turn_id}` 向前分页 |
| `GET/POST` | `/v1/threads/{id}/queue` | 读取或追加 TUI/Web 共享的持久后续消息队列 |
| `DELETE` | `/v1/threads/{id}/queue/{message}` | 删除尚未开始派发的队列消息 |
| `POST` | `/v1/threads/{id}/queue/{message}/retry` | 重试 daemon 中断后需确认的队列消息 |
| `POST` | `/v1/threads/{id}/turns` | 启动 turn；支持 mode 与图片 attachments |
| `POST` | `/v1/threads/{id}/turns/{turn}/plan` | 批准、修改或取消当前 Plan Ticket |
| `POST` | `/v1/threads/{id}/checkpoints/{checkpoint}/preview` | 预览恢复 checkpoint 的文件与上下文影响 |
| `POST` | `/v1/threads/{id}/checkpoints/{checkpoint}/rewind` | 按显式 scope 恢复文件、上下文或两者 |
| `GET` | `/v1/turns/{id}` | 读取单个 durable turn |
| `POST` | `/v1/turns/{id}/interrupt` | 中断活动 turn |
| `POST` | `/v1/turns/{id}/steer` | 注入指令，body: `{"content":"..."}` |
| `GET` | `/v1/turns/{id}/items` | 读取 durable turn items |
| `GET` | `/v1/turns/{id}/receipt` | 读取可离线重建的 Turn Receipt |
| `POST` | `/v1/permissions/{request_id}` | 回答审批；必须提供 `session_id`，支持 `decision`、`patch_plan_id` 与 `selected_hunks` |
| `POST` | `/v1/questions/{id}` | 回答结构化 Agent 提问 |
| `POST` | `/v1/artifacts/images` | 上传有界图片到内容寻址 ArtifactStore |
| `GET` | `/v1/artifacts/{sha256}` | 分页读取完整工具 Artifact |
| `GET` | `/v1/workspace/files` | 有界列出或搜索当前工作区文件 |
| `GET` | `/v1/workspace/file` | 有界读取当前工作区文本文件 |
| `GET` | `/v1/workspace/diff?scope=all&path=.` | 读取工作区 diff；scope 可为 all/staged/unstaged |
| `POST` | `/v1/workspace/stage` | 以审查 digest stage 显式路径 |
| `POST` | `/v1/workspace/commit` | 以 staged digest 创建本地 commit，不 push |
| `GET/POST` | `/v1/providers` | 读取 Catalog 或 Doctor 后保存路由 |
| `GET/DELETE` | `/v1/providers/{id}` | 读取或删除非活动 Provider 路由 |
| `POST` | `/v1/providers/{id}/activate` | 激活已通过 readiness 的路由 |
| `GET/POST` | `/v1/goals` | 查询或创建有界 Goal |
| `GET` | `/v1/workers` | 查询当前会话 Worker |
| `POST` | `/v1/workers/{id}/followup`、`review`、`apply`、`cancel` | 跟进、审查、应用或取消 Worker |
| `GET/POST` | `/v1/memories` | 查询或新增 Memory |
| `PATCH/DELETE` | `/v1/memories/{id}` | 编辑、置顶、过期或删除 Memory |
| `GET/PATCH` | `/v1/memory/settings` | 读取或更新自动记忆设置 |
| `GET` | `/v1/skills`、`/v1/mcp` | 稳定高级抽屉状态 |
| `GET` | `/v1/capabilities` | 查询协商能力 |
| `GET` | `/v1/usage` | 汇总 durable token usage；未知价格返回 `unknown` |

创建 turn 返回 `202 Accepted`。返回的 turn 已经写入 durable runtime，后续可以立即通过
items、events 或 receipt 查询。

`/v1/capabilities` 除 API/事件/stream-json 版本外，还返回 `feature_flags.stable/labs/internal`、
`labs_enabled` 和当前宿主的 sandbox capability/state。`feature_flags.labs` 表示代码中存在实验能力；
`labs_enabled=false`（默认）表示当前进程没有激活这些控制面。调用方必须同时协商级别与激活状态，不能
仅因命令模型存在就调用 Labs。Windows 探针成功时返回
`state=partial_enforcement`、`windows_forced_sandbox=partial`；失败时返回 `unavailable`。客户端必须展示
partial 的读取/网络限制，不能把它描述为完整隔离。

## SSE 事件

```text
GET /v1/threads/{thread_id}/events?after_seq=42
Accept: text/event-stream
```

每条事件包含 durable `id`（即 thread 内递增 `seq`）、事件类型和完整 JSON data。断线后将
最后收到的 id 作为 `after_seq`，或通过 `Last-Event-ID` header 重连；服务只返回严格大于该
游标的事件，因此不会重复已确认事件，也不会跳过已提交事件。

首次打开长会话时可以在 `after_seq=0` 的同时传 `tail=1..5000`，只回放当前高水位前最近一段事件；
后续重连必须传最后确认的非零 `after_seq`，此时 Core 忽略 `tail` 并严格从游标续接，避免断线窗口丢事件。

`run.finished` schema 1 已增加可选 `outcome`、`failure_category`、`changes`、`verification` 和
`result_summary`。当前 Runner 会填写统一 outcome、稳定失败分类和有界结果摘要；`changes` 与
`verification` 只有在发布端有可证明的结构化证据时才会出现。产品结果卡仍以 Turn Receipt 与其他
durable 事件为权威，并在字段不可证明时显示 unavailable。schema 1 调用方必须把所有新增字段当作可选。
`status` 保留兼容用的粗粒度状态；新客户端应优先读取 `outcome`。`tool_use`、`length`、`incomplete`
映射为“不完整”，`cancelled` 映射为“已中断”，`content_filtered` 与 `transport_error` 保持独立且不能
并入成功。

## Turn Receipt

Receipt 只使用 SQLite 中的 TurnRecord、TurnItemRecord 和 RuntimeEventRecord 构建，Core
重启后仍可读取。内容包括：

- 实际 route、model 和 wire format；
- mode、authority、workspace trust、sandbox 与允许动作；
- 起止时间、状态、token usage 和成本；
- 工具与审批计数、成功修改工具对应的文件、逐文件 additions/deletions、checkpoints、artifacts 和 workers；
- diagnostics/verification evidence 与错误分类。

Receipt schema 1 还可选保存原始 `outcome`、`failure_category` 和有界 `result_summary`。字段缺失表示
旧记录或证据不可得，不能从 legacy status 猜测。

文件改动只计入成功的写工具结果；失败调用和 `apply_patch` dry-run 不会冒充修改。旧事件没有行数时，
对应 additions/deletions 保持 `null` 并在 `change_line_stats` 中标记 unavailable，不会拿查询时的当前
workspace diff 回填历史 Turn。无法从 durable records 证明的其他字段同样列入 `unavailable`。模型价格未
配置时 `cost` 固定为 `unknown`。

## Python SDK

`code_rook.sdk.CodeRookClient` 和 `AsyncCodeRookClient` 对上述 durable HTTP/SSE 契约提供
同步与异步封装，包括 thread/turn、事件游标重连、interrupt/steer、receipt、usage、diff 与
逐 hunk 审批。两种客户端都提供 `capabilities()` 和 `usage()`。SDK 不维护第二套会话状态；
调用方应持久化最后确认的 SSE `seq` 并在重连时传回。
