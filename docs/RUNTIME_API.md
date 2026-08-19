# Runtime API v1

CodeRook Core 在统一的 durable Thread/Turn/Item/Event runtime 上提供本地 HTTP/JSON 与
SSE 接口。默认监听 `127.0.0.1:7438`；TUI、IPC 和 HTTP 读取同一个
`~/.coderook/runtime.db`，不会分别维护近似状态。

## 安全绑定

- 回环地址默认可直接访问，也可以设置 token。
- 监听 `0.0.0.0`、局域网地址或其他非回环地址时，必须设置
  `CODEROOK_API_TOKEN`，否则 Core 启动失败。
- token 只从环境变量读取，不写入项目配置、日志、事件或 Turn Receipt。
- 配置 token 后，每个请求都需要 `Authorization: Bearer <token>`。

```powershell
$env:CODEROOK_API_HOST = "127.0.0.1"
$env:CODEROOK_API_PORT = "7438"
uv run coderook-core
```

## JSON 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/v1/threads` | 列出 durable threads |
| `POST` | `/v1/threads` | 创建 thread，body: `{"title":"...","mode":"chat"}` |
| `GET` | `/v1/threads/{id}` | 读取单个 durable thread |
| `PATCH` | `/v1/threads/{id}` | 更新标题或归档，body: `{"title":"...","archived":true}` |
| `GET` | `/v1/threads/{id}/turns` | 列出 thread 的 durable turns |
| `POST` | `/v1/threads/{id}/turns` | 启动 turn，body: `{"content":"...","mode":"act"}` |
| `GET` | `/v1/turns/{id}` | 读取单个 durable turn |
| `POST` | `/v1/turns/{id}/interrupt` | 中断活动 turn |
| `POST` | `/v1/turns/{id}/steer` | 注入指令，body: `{"content":"..."}` |
| `GET` | `/v1/turns/{id}/items` | 读取 durable turn items |
| `GET` | `/v1/turns/{id}/receipt` | 读取可离线重建的 Turn Receipt |
| `POST` | `/v1/permissions/{request_id}` | 回答审批；支持 `decision`、`patch_plan_id` 与 `selected_hunks` |
| `GET` | `/v1/workspace/diff?scope=all&path=.` | 读取工作区 diff；scope 可为 all/staged/unstaged |
| `GET` | `/v1/capabilities` | 查询协商能力 |
| `GET` | `/v1/usage` | 汇总 durable token usage；未知价格返回 `unknown` |

创建 turn 返回 `202 Accepted`。返回的 turn 已经写入 durable runtime，后续可以立即通过
items、events 或 receipt 查询。

## SSE 事件

```text
GET /v1/threads/{thread_id}/events?after_seq=42
Accept: text/event-stream
```

每条事件包含 durable `id`（即 thread 内递增 `seq`）、事件类型和完整 JSON data。断线后将
最后收到的 id 作为 `after_seq`，或通过 `Last-Event-ID` header 重连；服务只返回严格大于该
游标的事件，因此不会重复已确认事件，也不会跳过已提交事件。

## Turn Receipt

Receipt 只使用 SQLite 中的 TurnRecord、TurnItemRecord 和 RuntimeEventRecord 构建，Core
重启后仍可读取。内容包括：

- 实际 route、model 和 wire format；
- mode、authority、workspace trust、sandbox 与允许动作；
- 起止时间、状态、token usage 和成本；
- 工具与审批计数、修改文件、checkpoints、artifacts 和 workers；
- diagnostics/verification evidence 与错误分类。

无法从 durable records 证明的字段会列入 `unavailable`，不会伪造为已知事实。模型价格未
配置时 `cost` 固定为 `unknown`。

## Python SDK

`code_rook.sdk.CodeRookClient` 和 `AsyncCodeRookClient` 对上述 durable HTTP/SSE 契约提供
同步与异步封装，包括 thread/turn、事件游标重连、interrupt/steer、receipt、usage、diff 与
逐 hunk 审批。SDK 不维护第二套会话状态；调用方应持久化最后确认的 SSE `seq` 并在重连时传回。
