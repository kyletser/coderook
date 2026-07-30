# CodeRook PC 桌面版移植计划

更新时间：2026-07-16

## 1. 结论

推荐采用以下路线：

- 桌面壳：Tauri。
- 前端：React + TypeScript + Vite。
- Agent Runtime：保留现有 Python Core，不重写 Agent loop。
- Python 交付：先用 PyInstaller `onedir` 构建 Core sidecar，稳定后再评估 `onefile`。
- 进程桥：Rust 启动并监管 sidecar，Rust 持有本地认证 token 和 JSON-RPC 连接。
- UI 不直接访问 Python TCP 端口，也不持有 LLM API key。

选择这条路线的核心原因不是“前端技术更新”，而是当前项目已经把 UI 与 Core 解耦。桌面版应当
成为协议的新客户端，继续复用 Python 中的 Session、Permission、Tool、MCP、Trace 和 Agent loop。

PyInstaller 可以把解释器和依赖一起交付；Tauri 支持把外部二进制作为 sidecar 随安装包分发。
首版使用 `onedir`，便于诊断动态 import、MCP 和资源文件缺失问题。

## 2. 开工门槛

桌面壳技术验证可以立即开始，但对外发布必须满足：

1. Phase 0 全部完成。
2. 至少具备 Glob、Grep、Edit、GitDiff。
3. 支持 `run.cancel`，关闭窗口时能够可靠终止或转入后台。
4. Core 具备 loopback 限制和随机 token 认证。
5. Session resume、interrupted run 和 transcript 恢复有 E2E。
6. Windows 安装包在无 Python、Node、Rust 的干净机器上通过 smoke test。

当前已经满足 Glob/Grep/Edit/GitDiff、`run.cancel`、Session/transcript 恢复、完整 Session 生命周期、
TUI picker、本地 IPC loopback 和随机 token 认证；Windows/Ubuntu CI 已配置，开发机 wheel smoke 已通过。
对外发布前仍需取得远端双平台绿灯，并完成无 Python 环境的 Windows 安装包 smoke test。

## 3. 目标架构

```text
┌──────────────── CodeRook Desktop ────────────────┐
│ React/TypeScript UI                                │
│ sessions · timeline · diff · tasks · permissions  │
└──────────────────────┬─────────────────────────────┘
                       │ typed Tauri commands/events
┌──────────────────────▼─────────────────────────────┐
│ Rust Desktop Bridge                                │
│ sidecar lifecycle · auth token · RPC · file dialog │
│ crash restart · tray/window state · update         │
└──────────────────────┬─────────────────────────────┘
                       │ authenticated JSON-RPC
┌──────────────────────▼─────────────────────────────┐
│ Python CodeRook Core sidecar                           │
│ Agent loop · tools · permission · sessions · MCP   │
│ trace · checkpoint · provider                      │
└──────────────────────┬─────────────────────────────┘
                       │ scoped subprocesses/files
                 project workspace
```

依赖方向：

- WebView 只调用经过 allow-list 的 Tauri command。
- Rust Bridge 是 sidecar 的唯一父进程和连接持有者。
- Python Core 不知道 React 组件，也不依赖桌面框架。
- Desktop/TUI/CLI 使用同一套协议模型和事件语义。

## 4. 建议目录

```text
coderook/
├─ src/code_rook/          # 现有 Python Core/CLI/TUI
├─ desktop/
│  ├─ src/                   # React UI
│  │  ├─ features/session/
│  │  ├─ features/chat/
│  │  ├─ features/diff/
│  │  ├─ features/permission/
│  │  └─ lib/protocol/
│  ├─ src-tauri/
│  │  ├─ src/                # Rust bridge 与 sidecar supervisor
│  │  ├─ capabilities/       # 最小权限 allow-list
│  │  └─ binaries/           # CI 生成的平台 sidecar
│  └─ tests/
├─ packaging/
│  ├─ pyinstaller/
│  └─ scripts/
└─ docs/
```

## 5. UI 信息架构

第一版直接进入工作台，不做营销首页。

```text
┌ Sessions ─────┬ Conversation / Run Timeline ──────┬ Context ─────┐
│ search        │ user / assistant / tool / approval│ changed files│
│ recent        │ streaming response                │ tasks        │
│ interrupted   │ inline diff / test output         │ checkpoints  │
│ + new         │                                    │              │
├───────────────┴────────────────────────────────────┴──────────────┤
│ workspace · model · permission mode · context · cost · status    │
├───────────────────────────────────────────────────────────────────┤
│ composer                                      send / stop         │
└───────────────────────────────────────────────────────────────────┘
```

核心交互：

- 左侧：Session 搜索、恢复、新建、重命名、fork。
- 中间：流式对话和 run timeline；工具调用可折叠，审批原位出现。
- 右侧：changed files、diff、tasks、后台进程和 checkpoints 标签页。
- 底部：workspace、模型、权限模式、上下文水位、token/cost、连接状态。
- 运行中主操作从 Send 切换为 Stop；取消成功后显示明确终态。
- Permission 使用聚焦面板，展示工具、目标路径/命令、影响范围和四种决策。

## 6. 分阶段工作

### D0：协议和 sidecar 可交付性验证（2-4 天）

任务：

1. 增加 `core.hello`：protocol version、capabilities、instance ID。
2. Core 支持 `--port 0`、`--auth-token`、`--parent-pid` 和结构化 ready 消息。
3. 复用现有强制 loopback + 首帧 token 握手；Rust 通过 `CODEROOK_IPC_TOKEN` 注入临时凭据。
4. 写 PyInstaller spec，先产出 Windows `onedir` Core。
5. 建最小 Tauri app，由 Rust 拉起 Core、ping、停止并收集 stderr。

验收：无 Python 环境的 Windows VM 能启动桌面壳，Core ready 后完成 ping，退出后无残留进程。

### D1：桌面协议客户端与 Session 工作台（5-8 天）

任务：

1. Rust 实现单连接 RPC multiplexer、事件转发、断线和指数退避。
2. TypeScript 生成/维护 protocol types，拒绝 `any` 穿透到组件。
3. 复用现有 session list/create/resume/history/rename/fork/export/delete/close 协议。
4. 实现对话 timeline、流式 token、工具块和权限审批。
5. 工作区选择使用系统目录对话框，Core 接收显式 workspace root。

验收：新建、退出、重启、恢复同一 Session；审批后 run 正常继续；Core 崩溃时 UI 给出可恢复状态。

### D2：Coding Agent 桌面闭环（7-12 天）

任务：

1. 对接 Glob/Grep/Edit/ApplyPatch/GitDiff 事件。
2. changed-files 列表和 diff viewer。
3. checkpoint 创建、预览和 rewind 确认。
4. Stop 按钮调用 `run.cancel`，显示 cancelling/cancelled 状态。
5. tasks/background 面板和 test output 展开视图。
6. 拖入文件只生成受工作区边界约束的引用，不直接把任意路径交给模型。

验收：完成“检索 -> 多文件编辑 -> 测试 -> diff -> rewind”，取消后进程树为空。

### D3：设置、安全和诊断（5-8 天）

任务：

1. API key 写入 OS 安全存储；前端状态中只保留 provider 是否已配置。
2. Provider/model/permission mode/MCP 设置页。
3. Trace 默认脱敏，诊断包二次确认后导出。
4. CSP、Tauri capability 和 shell sidecar 参数使用最小 allow-list。
5. 单实例、窗口状态恢复、托盘行为和异常退出恢复。
6. Core/desktop 日志轮转、磁盘配额和“打开日志目录”。

验收：DevTools、日志和 trace 中均搜不到测试 secret；未经授权的 WebView command 被拒绝。

### D4：安装、更新和发布（5-8 天）

任务：

1. Windows x64 安装包、卸载和用户数据保留策略。
2. sidecar 与 Desktop 版本兼容矩阵；升级前 session schema migration。
3. 代码签名、更新签名、回滚和离线安装包。
4. Windows CI 构建 sidecar 和 installer；Linux/macOS 后续各自原生构建。
5. 干净 VM、中文路径、空格路径、代理、休眠唤醒、断网和杀进程测试。

验收：安装、首次配置、执行任务、升级、恢复 Session、卸载全流程可重复通过。

## 7. 进程与协议细节

启动顺序：

1. Desktop 生成一次性随机 token 和预留实例目录。
2. Rust 启动 Core sidecar，传入 token、workspace policy 和 parent PID。
3. Core 绑定随机 loopback 端口，在 stdout 输出单行 ready envelope。
4. Rust 建立连接并执行 `core.hello`；版本不兼容时停止 sidecar。
5. React 收到 `core-ready` 后再加载 sessions。

退出策略：

- 无运行任务：刷新 session meta，优雅停止 Core。
- 有运行任务：询问“取消并退出 / 转后台 / 返回”；首版可以只提供前两项。
- Core 无响应：先取消进程树，再保留 `interrupted` 元数据供下次恢复。

协议必须补充：

- `protocol_version`、`capabilities`、`event_seq`、`instance_id`。
- command timeout、最大 frame、错误分类和客户端 identity。
- `run.cancel`、`session.rename/fork/export`、`core.shutdown`。
- 事件重连使用 `last_event_seq`，不能只依赖某个 run 的 JSONL 回放。

## 8. 数据与安全

建议逻辑目录：

```text
app-data/
├─ config.toml
├─ sessions/
├─ checkpoints/
├─ logs/
├─ traces/
└─ extensions/
```

安全约束：

- API key 不进入 `.env`、React localStorage、IPC trace 或 crash report。
- Desktop 传给 Core 的 workspace root 必须是用户明确选择的真实目录。
- Core、Bash、MCP 子进程继承同一 workspace/permission policy。
- 打开外链、Shell、文件系统和 sidecar 均通过最小 Tauri capability。
- 不以管理员权限运行桌面应用或 Python sidecar。

## 9. 测试矩阵

| 层级 | 必测内容 |
|---|---|
| Python unit | Agent、Session、Tool、Permission、Boundary、protocol model |
| Rust unit | sidecar state machine、RPC correlation、auth、crash restart |
| Frontend unit | timeline reducer、permission state、diff state、session state |
| Contract | Python command/event schema 与 TypeScript types 一致 |
| Desktop E2E | 启动、对话、审批、取消、恢复、diff、rewind |
| Packaging | 无开发环境机器安装和运行，sidecar/资源完整 |
| Security | token 拒绝、路径逃逸、secret redaction、capability deny |
| Soak | 多 Session、长输出、休眠唤醒、断线重连、24h 磁盘增长 |

## 10. 预估与人员安排

单人开发、Core Phase 0/1 能力已具备的前提下，Windows MVP 约 4-6 周：

| 周期 | 交付 |
|---|---|
| 第 1 周 | D0 sidecar spike + protocol/auth |
| 第 2 周 | D1 Session、timeline、stream、permission |
| 第 3 周 | D2 diff、changed files、cancel、checkpoint |
| 第 4 周 | D3 settings、安全存储、诊断 |
| 第 5-6 周 | D4 安装、签名、E2E、稳定性和发布文档 |

如果同时补 Agent Phase 0/1，桌面工作应与 Core 交错推进，总周期更现实地按 8-12 周安排。

## 11. 风险与降级方案

| 风险 | 首选处理 | 降级方案 |
|---|---|---|
| PyInstaller 动态依赖遗漏 | 固定 spec + bundled smoke test | 首版随应用分发嵌入式 Python 目录 |
| sidecar 启动慢 | `onedir`、ready splash、延迟加载 MCP | 首版不启用自动 MCP |
| Rust 桥开发成本 | 只负责 lifecycle/RPC，不承载业务 | 短期 React 直连 loopback，但只用于内部 Demo |
| 协议快速变化 | protocol version + generated types | Desktop 与 Core 同版本强绑定 |
| 安装包体积 | Tauri + sidecar 去除 dev 依赖 | 接受首版体积，先保证可诊断性 |
| 跨平台差异 | 每个平台原生 CI 构建 | Windows-first，macOS/Linux 延后 |

## 12. 官方实现依据

- Tauri external binary / sidecar：<https://v2.tauri.app/develop/sidecar/>
- Tauri distribution：<https://v2.tauri.app/distribute/>
- PyInstaller operating mode：<https://pyinstaller.org/en/stable/operating-mode.html>
