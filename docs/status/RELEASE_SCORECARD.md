# CodeRook 发布评分卡

**更新时间**：2026-08-24

**代码锚点**：`main@2a8da99` + 当前未提交的 Harness 架构改造工作树

候选状态：**NO-GO（不是 v1.0.0 Release Candidate）**

当前改造尚未形成一个已提交、由 required CI 验证的候选 commit。本文只区分当前代码事实和仍需外部
执行的证据门禁；workflow、测试、适配器或发布脚本存在，都不等于相应门禁已经通过。包版本仍为
`0.1.0` Alpha，未创建 `v0.9.0-beta.1`、`v1.0.0-rc.1` 或 `v1.0.0` tag。

## 1. 当前代码事实

- 仓库 `.env` 永不自动加载。显式 `--env-file` 禁用插值、不修改 `os.environ`、不能设置
  `CODEROOK_CONFIG`，并作为只读 credential overlay 供 Core、TUI/CLI Provider/Doctor 和 WebSearch
  共用；进程环境优先，凭据不经 IPC。无法证明已运行 daemon 使用同一 overlay 时 fail closed。
- Provider Catalog 统一 DeepSeek、OpenAI、Anthropic、Gemini、Kimi/Moonshot、OpenRouter、
  SiliconFlow、Ollama 与 LM Studio，并支持三个自定义 wire format；readiness 在创建 run 前阻止不可用
  route，Doctor 通过后才原子提交配置。前置备份与迁移收据是独立完整性证据；完成收据配空 Catalog、
  收据冲突/损坏或首次迁移半提交均失败关闭，Route 写入会在收据失败时回滚原字节。
- Runtime API 对包括 loopback 在内的所有请求强制 Bearer；空/纯空白环境值不能关闭鉴权。未配置时
  no-follow 加载或排他创建用户 token，POSIX 校验 owner/0600，Windows 校验 reparse、普通文件及对象
  身份而不宣称额外 ACL 隔离。
- 每个 Turn 冻结 authority、route/model、工具、图片、并行、thinking 与 sandbox capability。修改另一
  session 或运行中配置不能扩大当前 Turn，子 Agent authority 只能收窄。
- Windows Shell/Run 没有强制 OS sandbox，始终保留显式审批；Linux bwrap/macOS Seatbelt 只有真实探针
  成功才启用，且不再暴露整个宿主根目录。Shell 环境过滤常见 API、云、Git 与 SSH 凭据变量。
- Anthropic Messages、OpenAI Chat 与 OpenAI Responses 使用统一终止状态；length、incomplete、提前
  EOF、content filter、失败和取消不会误报成功，被截断的工具参数不会执行。
- 持久 Shell、前台/后台输出使用有界缓冲；大输出进入 Artifact。Event/Runtime 持久化失败触发
  `audit_degraded` 并暂停非 READ 工具，Trace 降级单独可见。
- Session Ledger 现以 v2 事实事件投影输入、模型消息、请求快照和执行事件；Provider 调用前验证持久
  `RequestSnapshot` 与实际消息/System/Tool Schema/Route/执行契约完全一致。run/step/tool 事件在广播前
  关联 `ledger_seq`，关键持久化失败会阻止事件继续传播。新写入已收口为纯 v2 SessionEvent；旧
  message/block 前缀只读兼容且不会继续双写。
- TUI 使用 session-scoped durable cursor 和统一 reducer，重连按 `after_seq` 回放；daemon 全局事件不
  混入 thread 时间线。活动 Turn resume busy 时只读附着权威 thread，恢复 transcript、订阅和未决交互；
  切换 session 会撤销旧 thread 订阅。无模型时 onboarding 非阻塞，readiness 失败保留草稿且不创建失败 run。
- 稳定 TUI shell、命令、选择器、审批、管理面板、事件和结果卡已接集中式 `zh-CN`/`en-US` 文案；
  结果卡把 tool-use/length/incomplete 显示为不完整、cancelled 显示为中断，并保留 content-filtered 与
  transport-error 的独立失败语义；Plan approve/revise/cancel 使用 durable `plan.respond/plan.resolved`，
  重启不复活已解决审批；Labs Workflow 图、协议状态值、日志和第三方动态文本保留技术原文。
- Change Center 是全屏可聚焦 overlay，合并当前 diff 与 durable Receipt，支持 file/hunk、rename/mode 与
  opaque metadata 导航。`state_digest` 是绑定 scope、canonical visible payload、exact ref/commit、index、
  tracked/untracked 内容及 untracked mode 的审查令牌；不透明 tracked 内容必须有 old/new blob 长度与
  SHA-256 证据，子目录和特殊 Git 路径无法精确归属时 `review_complete=false`。
  stage 在真实 index lock 下保留 sparse/split/skip/assume 语义，只发布点名内容并返回 staged 令牌；commit
  前 TUI 强制展示该最终 staged payload 并要求独立确认；commit 用 exact-ref CAS 提交并回读真实文件
  列表，支持初始提交，取消歧义时安全探测/回滚。整个 workspace 有
  活动 Turn、边界外 staged 内容、截断/证据不足、detached HEAD、竞态或令牌漂移都失败关闭；不运行
  hook/signing，也不 push。
- 有界 Goal 是 stable capability：新 Goal 默认最多总计 3 个 Turn/1800 秒，可设硬 token budget 和更小
  权限 ceiling。没有 completion criteria 时仍在边界内继续；只有 daemon 验证引用或用户显式验收可以
  完成，重启后进入 `paused_needs_confirmation`。
- 基础 WorkerController 是 stable capability：start/status/peek/followup/retry/cancel/review/apply 全部
  session-scoped；route/readiness、模型、预算、profile 与权限边界冻结。可写 Worker 强制进入受管
  worktree，只有 verification、完整 Diff、人工 review digest 和干净基线都重验通过后才能显式 apply；
  冲突、越权和摘要漂移 fail closed，apply 不 stage/commit/push。
- Agent Preset 冻结进 Session Header；standard/minimal 稳定，切换通过 fork。Tool Presentation 由 Action
  Manifest 纯转换并持久化，TUI 按类型通用渲染。声明式 Tool Program 与 ACP 外部 Worker 已接入，但均
  保持 Labs：前者禁止任意代码并把子调用重新送入工具管线，后者强制 worktree、能力不支持时明确失败，
  当前仍缺独立人工互操作与安全报告。
- Capability Kernel 已实际承载 workspace 级 Provider/MCP/Hooks/Worker Backend 和 session 级 Tool
  Registry；文本、工具成功与权限拒绝三个 keyless Golden Replay 固定验证重开后的 Ledger 顺序和模型
  消息投影。
- Runtime SQLite 当前数据库 schema 为 v4，公开 Thread/Turn/Item/Event/Facade 逐行 schema 仍为 1；
  未来数据库/逐行版本和外键损坏失败关闭。Runtime Doctor 严格区分只读 inspect 与显式 repair，并分别
  报告备份、迁移收据、fallback credential 和 Route Catalog 状态；repair 不猜测损坏内容。
- `feature_flags` 把 Goal、基础子 Agent、Skills、MCP Tools、Memory 和 Change Center 标为 stable；Tool Program、ACP Worker backend、Fleet、
  声明式 Workflow、Hooks v2、MCP Resources/Prompts 和 VS Code 原型标为 Labs。Labs 默认关闭且隐藏，
  仅 `CODEROOK_LABS=1` 激活；关闭时不加载 Hook，也不暴露或恢复 Workflow/Fleet 控制面。
- 日常自动化收敛为一个 10 分钟目标的 Ubuntu required job。安全、恢复、MCP、分发与真实模型矩阵只由
  `workflow_dispatch` 或 release tag 触发，没有 cron/nightly push matrix。
- release workflow 已准备 PyPI Trusted Publishing、五个平台 archive、GHCR、SBOM、checksum、
  provenance 与签名；Homebrew/Scoop 目前只是待生成的 Release asset，不是已经上线的 tap/bucket。

## 2. 当前验证边界

未提交工作树上的局部或完整本机结果只用于改造验收，不能提升为发布候选证据。真正的候选 commit 仍须
从头连续运行 Ruff、品牌、公开仓库、Windows/Linux Mypy、全量 pytest、协议检查、build 和 wheel smoke，
并取得同一 SHA 的 required CI。本文不长期保存易失真的测试总数，也不把工作树或局部检查写成“当前
提交全绿”；本轮本机结果由交付记录说明，候选状态只看下表。

## 3. v1.0.0 硬门禁

| 门禁 | 要求 | 当前结果 |
|---|---:|---|
| 最终候选本地门禁 | Ruff、品牌、公开仓库、双平台 Mypy、全量 pytest、协议、build、wheel smoke 连续通过 | **尚无最终候选连续结果** |
| Required CI | `Required Ubuntu gate` 对候选 commit 通过 | **尚无候选 commit 结果** |
| P0/P1 | 已知 P0/P1 为 0 | **未提交工作树已独立复核清零；尚无候选 commit 绑定审计** |
| 总体 pass@1 | 内建 50 任务 ≥80% | **未运行真实模型候选集** |
| 多文件 pass@1 | ≥75% | **未运行** |
| 只读 pass@1 | ≥90% | **未运行** |
| 两种 wire format × 两次 | 四份原始报告及聚合报告 | **未产生** |
| Aider/SWE-bench | 固定切片、官方 harness、完整 artifact | **未产生** |
| 安全负例 | 当前候选三平台 100% | **没有当前候选外部矩阵** |
| 强杀恢复 | 三平台各 100 次、≥95%、高风险孤儿进程 0 | **没有当前候选报告** |
| 双 session 与重连 | 各 100 次；事件/审批/取消污染 0，游标不重不漏 | **没有发布级矩阵** |
| Goal 产品验收 | 完成、预算耗尽、暂停、取消、重启恢复与证据不足场景 | **代码路径已实现；缺最终候选端到端证据** |
| Agent Control 产品验收 | session 隔离、Worktree、Diff/Review/验证/apply、冲突 fail closed | **代码路径已实现；缺最终跨平台冲突矩阵** |
| TUI 产品验收 | 80×24、100×30、140×40；中英文；成功/失败/取消完整闭环 | **稳定界面代码已对齐；人工与自动产品矩阵未完成** |
| 三平台安装 | 五个 portable、PyPI、安装脚本、Homebrew/Scoop 渠道 smoke | **workflow 已准备，真实发行/渠道未发布** |
| 首次用户成功 | 10 名新用户至少 8 名在 10 分钟内独立完成有效任务 | **未开展** |
| 公开发行与供应链 | tag、GitHub Release、PyPI、GHCR、SBOM、checksum、provenance、签名 | **均未产生真实发行证据** |
| 跨发布升级 | 两个真实 tag 之间升级、备份恢复与回滚 | **未运行** |

## 4. 已知边界

- Windows 的正式目标是 TUI 可用，不是强制 sandbox；Shell/Run 必须 Ask-only。
- 域名级 Shell 出站白名单没有可接受的 OS 强制后端，当前 fail closed。
- Labs 默认关闭；即使启用，其 UX/恢复语义也不属于稳定合同。
- Labs Workflow 图、协议状态值、日志正文和 Provider/Skill/MCP 动态文本不翻译。
- TUI 图片入口主要接收本地图片路径，不保证所有终端都能读取剪贴板原始位图。
- Python/TypeScript Diagnostics 与大型 monorepo 索引仍缺跨真实项目性能/可靠性报告。
- VS Code 不发布 Marketplace，也不阻塞 v1。
- 当前没有 dependency-review required job；远端 Actions、ruleset 与包渠道必须由 GitHub/API 和真实安装
  证明，不能从仓库文件推断。

## 5. 发布结论

当前代码已经补齐多项 v1 产品和安全闭环，但证据门禁仍明确为 **NO-GO**。表中全部硬门禁取得绑定
同一候选 commit 的可复现证据之前：

- 不创建稳定 `v1.0.0` tag；
- 不宣称 production-ready；
- 不把 workflow、fixture、旧 commit 或局部测试写成当前候选的外部成绩；
- 不把生成脚本或 Release asset 写成已经上线的安装渠道。
