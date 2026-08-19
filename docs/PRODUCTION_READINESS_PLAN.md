# CodeRook 生产就绪改造计划

**版本**：1.0

**日期**：2026-08-18

**代码基线**：CodeRook 0.1.0，commit `291c8a0`

**输入**：`FUNCTIONAL_ARCHITECTURE.md` §20、`OPTIMIZATION_ROADMAP.md` Stage 0–4 复盘

**目标定位**：从“功能合格的本地 Coding Agent”推进到“可公开 Beta、可在受控团队中试用的可靠 Agent”

---

## 1. 结论

> 实施更新（2026-08-18）：R0-R5 的仓库内主要改造已落地；逐项证据与仍存代码缺口见
> `PRODUCTION_GAP_MATRIX.md`。真实模型 pass@1、Linux/macOS 远端安全结果、三平台进程级
> 100 次强杀恢复率和候选分发实装仍未产生，因此 `RELEASE_SCORECARD.md` 保持 NO-GO。

后续不再以功能数量作为主线。当前最缺的不是另一个工具或面板，而是三类证据：

1. **效果证据**：真实仓库任务是否能稳定完成，而不只是组件单测通过。
2. **安全证据**：自动放行是否由真实 OS 边界支撑，尤其是 Windows。
3. **运行证据**：崩溃、断线、续跑、成本和后台进程是否能被持久记录并正确恢复。

建议按 R0–R5 六个阶段推进。单人全职预计 10–14 周；两人可并行 R1/R2 与 R3，预计 7–10 周。
时间只是容量估算，不作为验收标准；每个阶段只有达到量化门禁才能结束。

### 阶段顺序

| 阶段 | 主题 | 建议周期 | 是否阻塞公开 Beta |
|---|---|---:|---|
| R0 | 真实任务 Benchmark 与发布评分卡 | 1–2 周 | 是 |
| R1 | 安全强制力与可审阅变更 | 2–3 周 | 是 |
| R2 | Durable 一致性、成本与进程治理 | 2–3 周 | 是 |
| R3 | 工具可靠性与感知能力收口 | 2 周 | 部分 |
| R4 | Headless 契约、SDK、MCP 与 IDE 验证 | 2–3 周 | 团队使用需要 |
| R5 | 产品化、配置统一与发布工程 | 1–2 周 | 是 |

---

## 2. “合格”的统一定义

### 2.1 公开 Beta 门禁

以下条件必须同时成立：

- 固定模型与固定配置下，真实任务集 pass@1 总成功率 ≥80%。
- 多文件修改类任务成功率 ≥75%，只读分析类任务成功率 ≥90%。
- 任何平台都不得出现未授权工作区外写入；安全负例必须 100% 通过。
- daemon 强杀/重启恢复场景成功率 ≥95%，不得产生孤儿 tool call 或静默丢失已确认写入。
- 每个 turn 都能离线重建 route、token、成本、审批、变更文件、验证结果和终态。
- Windows 没有真实隔离后端时，AUTO_REVIEW 的 shell 必须回落 ASK，不得宣称处于 sandbox。
- `coderook run --output-format stream-json` 能作为稳定机器接口使用，stdout 不混入日志。
- 完整 CI gate、wheel smoke、升级迁移测试在 Windows/Ubuntu 双平台通过。

### 2.2 暂不作为 Beta 门禁

- 一比一复刻 Claude Code/Codex。
- Desktop、云任务、插件市场和跨设备同步。
- 所有语言的常驻 LSP。
- 自动选择“最聪明”模型；路由只要求确定、可审计、可回放。
- `tui/app.py` 必须低于某个任意行数。结构边界和回归率比行数更重要。

---

## 3. R0：真实任务 Benchmark 与发布评分卡

> 实施状态（2026-08-18）：50 个任务、类别/预算/suite 契约校验、fixture 隔离、真实 `AgentRunner` 执行、文件改动审计、verifier、JSON/Markdown 报告和 quick/nightly/release workflow 已落地；尚未形成真实模型基线。
> 各工作项的实时证据与剩余门禁见 `docs/PRODUCTION_GAP_MATRIX.md`；未列为完成的项目不得仅凭路线图文本视为已交付。

### 3.1 目标

建立“Agent 是否真的能完成编码任务”的可复现证据。R0 完成前，后续优化不得以主观体验判断成功。

### 3.2 工作项

#### R0.1 Benchmark 目录与任务契约

新增：

```text
benchmarks/
├── tasks/                  # 每个任务一份 manifest
├── fixtures/               # 固定的小型真实仓库快照或生成器
├── expected/               # 机器可验证的不变式
└── README.md
scripts/run_benchmark.py
src/code_rook/benchmark/    # 任务加载、运行、评分、报告
```

任务 manifest 至少包含：

- `id`、语言、难度、类别、仓库基线 commit。
- 用户目标和允许的工具/权限姿态。
- 最大 step、token、美元、墙钟预算。
- 必须通过的测试、禁止改动的路径、期望变更范围。
- 成功判定器和失败分类。

首批不少于 40 个任务：

| 类别 | 数量下限 | 示例 |
|---|---:|---|
| 定位与解释 | 6 | 找根因、解释调用链、回答带证据 |
| 单文件修复 | 8 | 边界条件、异常处理、类型错误 |
| 多文件修改 | 10 | API + 实现 + 测试同步 |
| 测试与验证 | 6 | 补回归测试、修 CI、本地复现 |
| 重构 | 6 | 保持行为不变的模块拆分 |
| 安全负例 | 4 | 越界写、危险 shell、提示注入 |

至少覆盖 Python、TypeScript 和混合仓库；任务不得依赖实时网络内容。

#### R0.2 双层运行方式

- **PR 快速集**：10 个任务，固定 stub/provider，验证协议、工具链和评分器，目标 ≤15 分钟。
- **Nightly 真实模型集**：完整 40+ 任务，固定 route/model/version/temperature，保存原始 receipt 与报告。
- **候选发布集**：至少两个 wire format、两个代表性模型，各重复两次，报告 pass@1、波动与成本。

真实 API 密钥仅用于显式 nightly/release job；普通单元与集成测试继续不依赖密钥。

#### R0.3 指标与失败分类

必须记录：

- pass@1、测试通过率、首次修改正确率。
- 非目标文件改动数、回滚次数、审批次数。
- step、输入/输出/cache token、美元成本、墙钟时间。
- daemon 重启次数、重试次数、压缩次数。
- 失败分类：理解错误、检索失败、错误编辑、验证不足、权限阻塞、模型错误、运行时错误、预算耗尽。

报告同时输出 JSON 和 Markdown，写入构建产物，不提交每次运行的原始大文件。

#### R0.4 发布评分卡

在 `docs/RELEASE_SCORECARD.md` 维护最近一次候选发布的：

- commit、模型与配置指纹。
- 各类别成功率和成本分位数。
- 安全/恢复/兼容矩阵。
- 已知退化、豁免负责人和到期日期。

### 3.3 代码落点

- `src/code_rook/core/runner.py`：接受可复现 run 配置和 benchmark metadata。
- `src/code_rook/core/receipts/`：补齐评分所需证据字段。
- `src/code_rook/cli/commands/run.py`：支持机器输出，为 R4 复用。
- `.github/workflows/`：增加 quick/nightly/release 三档 job。

### 3.4 验收

- 40 个任务能从干净 fixture 一键运行并生成报告。
- 同一 stub 配置连续运行结果完全一致。
- 每个失败都有明确分类，不允许只有 `failed` 或 `llm_error`。
- 建立首个真实模型基线；未达到 80% 可以进入 R1/R2，但不得宣布公开 Beta。

---

## 4. R1：安全强制力与可审阅变更

### 4.1 Windows 沙箱决策

Windows Job Object 适合管理进程树、资源上限和整体终止，但不应被当作文件系统安全边界。
受限令牌可以移除 SID/权限；真正的 Win32 隔离需要 AppContainer/Win32 App Isolation 或等价边界。

先做两周以内的验证性原型，只允许以下三个结论之一：

1. **AppContainer helper 可行**：用小型原生 launcher 创建低权限进程，显式授予工作区能力，默认拒绝其他用户目录、注册表写和网络。
2. **WSL2 + bwrap 可行**：Windows 上将受控 shell 路由到 WSL/bwrap，并明确处理路径映射、Git、Python/Node 工具链和性能边界。
3. **均不可接受**：Windows 保持 degraded，AUTO_REVIEW shell 永久回落 ASK；公开文档明确 Windows 不具备自动 shell 隔离。

不得采用“Job Object + 杀进程树 = sandbox”的定义。

官方设计依据：

- [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- [Microsoft Application Isolation](https://learn.microsoft.com/en-us/windows/security/book/application-security-application-isolation)
- [Microsoft AppContainer implementation](https://learn.microsoft.com/windows/win32/secauthz/implementing-an-appcontainer)

### 4.2 Sandbox 执行契约

重构 `core/sandbox/`：

- `SandboxBackend` 协议：`probe()`、`plan()`、`spawn()`、`describe()`。
- 后端：`BwrapBackend`、`SeatbeltBackend`、验证后决定的 Windows backend、`DegradedBackend`。
- `SandboxPlan` 持久记录 backend、tier、workspace、network、allowed domains、domain policy enforced、writable roots、degraded reason 和策略版本。
- 所有 shell/Run/background/fleet worker 使用同一 spawn 边界，禁止各自拼 wrapper。
- AUTO_REVIEW 只有在 `plan.enforced is True` 且能力满足 action 时才能自动放行。

安全测试必须真实执行，而不只断言 argv：

- 工作区内允许的读写成功。
- 工作区外、用户凭据目录、系统目录写入失败。
- 禁网档位无法联网；允许网络档位按域策略执行。
- 子进程不能脱离进程树或 sandbox。
- symlink/junction、UNC、大小写、短文件名和路径规范化不能绕过。

### 4.3 Family 调用管线统一

当前 family 内部分派只经过家族层一次完整管线。改为：

1. 解析 family/action。
2. 生成规范化的 resolved invocation。
3. 对 resolved action **恰好一次**执行参数校验、hook、权限、sandbox、重试、输出策略和事件记录。
4. replay 仍接受旧工具名，但转换成相同 resolved invocation。

验收：平铺工具、family action、replay 三条入口产生相同权限决策、hook 事件和 receipt 语义。

### 4.4 逐 hunk 审阅

- 编辑工具先生成带稳定 hunk id 的 `PatchPlan`，包含 base hash。
- TUI 支持全选、全拒绝、逐 hunk 接受；选择结果返回 daemon，不在客户端直接写文件。
- daemon 依据 base hash 重建补丁；文件已变化则拒绝并重新生成预览。
- 审批 receipt 记录展示过的 patch hash、选择的 hunk 和最终落盘 hash。

### 4.5 验收

- 安全负例在 Windows/Linux/macOS 可用矩阵中 100% 通过。
- degraded 后端绝不产生 sandbox 自动放行。
- 每个 mutating action 的审批内容与最终写入可通过 hash 对应。
- family/replay 不存在绕过 hook、权限或输出策略的路径。

---

## 5. R2：Durable 一致性、成本与进程治理

### 5.1 单一事实流，而非立即合并存储

不建议现在把文件账本和 SQLite 强行合为一种存储。先把双真源约束变成可证明的不变式：

- 为 session transcript 增加单调 ledger sequence/checksum。
- Runtime 投影记录 source sequence 与 projection version。
- 启动时执行 `reconcile`：检测缺口、重复、状态冲突和孤儿 run，生成报告后再修复。
- 修复必须幂等；保留 repair journal，禁止静默覆盖。
- 增加 `coderook doctor runtime` 与只读 `--json` 输出。

### 5.2 Durable usage 与成本账本

- 将每次 `llm.usage` 归一化为持久 UsageRecord：model、route、wire format、input/output/cache token、pricing source/version、估算成本。
- RuntimeService 在同一 turn sequence 中持久投影 usage。
- TurnReceipt 从持久 usage 重建成本，不再依赖 TUI 内存累计。
- `cost_budget` router 从同一 usage ledger 读取累计值，避免展示和决策使用两套算法。
- pricing 覆盖记录来源和生效日期；未知价格显式 `unknown`，不得按 0 美元处理。

### 5.3 交互等待策略

- interactive 默认可无限等待，但 UI 必须显示等待时长和取消入口。
- headless 必须显式选择 `fail-fast`、超时秒数或预置答案策略，不允许无限挂起。
- daemon shutdown/客户端断线/turn interrupt 都要解析对应 Future，并写入终态原因。

### 5.4 Shell 与后台任务统一

提取统一 `ProcessSupervisor`：

- isolated shell、persistent shell、background job、hook、MCP、fleet 共用进程树、日志、取消和资源统计原语。
- persistent session 进入后台后仍保留 cwd/env，但命令执行有独立 job id。
- daemon 重启时不能假装恢复已经死亡的 OS 进程；统一落为 interrupted，并保留最后日志游标。
- Windows 使用 Job Object 做进程树治理，即使最终安全 sandbox 采用其他技术。

### 5.5 Artifact 生命周期

- 增加 `artifact.list/gc` 类型化命令和 `/artifacts` 管理面板。
- 默认只 dry-run；展示年龄、大小、引用来源和预计释放空间。
- 真删除需 `--yes`，删除前二次扫描引用并写 GC receipt。

### 5.6 崩溃矩阵验收

自动化覆盖以下中断点：

- user message 落盘前后。
- assistant block/tool call/tool result 各阶段。
- 文件写入 checkpoint 前、事务中、事务后。
- usage 到达但 turn 未终态。
- daemon shutdown、强杀、客户端断线、系统重启模拟。

验收：100 次故障注入中恢复成功率 ≥95%，其余必须明确 failed/interrupted，不能产生“看似成功但证据缺失”的终态。

---

## 6. R3：工具可靠性与感知能力收口

### 6.1 Web 工具

抽象 `WebSearchBackend`，至少支持：

- DuckDuckGo HTML 兜底。
- 可配置 SearXNG。
- 一个带 key 的结构化搜索后端，密钥走 CredentialStore。

要求：

- 结果统一为 title/url/snippet/source，保留 backend 与查询时间。
- 解析器 fixture 覆盖页面变化；单后端失败自动尝试明确配置的下一后端。
- WebFetch 保持逐重定向 SSRF 校验，并增加总下载字节、重定向次数、内容类型白名单。
- Benchmark 的核心任务不得依赖搜索结果实时变化。

### 6.2 多模态

- Route 增加显式 `supports_images`，factory/doctor 可探测或由用户覆盖。
- 不支持图片的 route 在调用前失败，给出可操作提示，而不是等待供应商报错。
- TUI 粘贴图片先落 ArtifactStore，只在发送时读取；显示尺寸、类型和 hash。
- 图片仍只进入一次模型请求，历史保留描述与 artifact handle。

### 6.3 诊断

- 把当前 pyright/tsc 一次性实现纳入统一 `DiagnosticsBackend`。
- 第一阶段只要求 Python/TypeScript 稳定、可取消、结果去重和增量文件过滤。
- Go/Rust 作为可选 backend；只有 benchmark 显示对应用户需求后才进入 Beta 门禁。
- 常驻 LSP 必须证明相对一次性 CLI 的延迟收益，否则不引入长期进程复杂度。

### 6.4 路由与价格收口

- 将 `is_brief_question()` 接入有测试的策略，或删除死代码；不保留“看似实现”的策略。
- 路由决策写入 receipt：候选、命中规则、成本阈值、fallback 原因。
- 价格表过期不影响执行，但 TUI/receipt 必须标注价格来源与日期。

### 6.5 验收

- Web 后端故障有稳定降级，不产生格式不明的空结果。
- 图片能力在发送前可判定，base64 不进入永久 transcript。
- 编辑后诊断的 P95 延迟不比当前基线恶化 20%，取消不遗留进程。
- 所有路由选择都可由持久证据离线解释。

---

## 7. R4：Headless 契约、SDK、MCP 与 IDE 验证

### 7.1 机器输出契约

新增：

```text
coderook run --output-format text|json|stream-json
```

- `text` 保持当前人类输出。
- `json` 只输出最终版本化 RunResult。
- `stream-json` 每行一个版本化 envelope：run lifecycle、token、tool、approval、usage、receipt。
- stdout 只承载协议；日志、进度和诊断写 stderr。
- 支持 `--event-filter`、`--include-partial`、`--resume` 和明确退出码。
- schema 从 pydantic 模型生成，并提供向后兼容测试与 golden fixtures。

### 7.2 Python SDK

先提供薄 SDK，不复制业务逻辑：

- `CodeRookClient`：thread/turn CRUD、interrupt、steer、event cursor、receipt。
- 同步和 async 两套最小入口；内部只消费 HTTP/SSE runtime API。
- 自动 Bearer auth、超时、重连和 Last-Event-ID。
- SDK 与 CLI stream-json 使用同一结果模型。

### 7.3 MCP 补全

MCP 已支持 stdio/TCP，以及 Streamable HTTP 的 tools 基础链路。Streamable HTTP 以单一 HTTP
endpoint 承载 POST/GET，并可用 SSE 传输服务端消息，已经取代旧 HTTP+SSE 传输。当前待补的是
GET 服务端消息、断线恢复/取消，以及 resources/prompts 独立能力。

工作项：

- transport 抽象统一 stdio、TCP、Streamable HTTP。
- session id、协议版本、断线恢复、请求取消和超时。
- TLS 校验、Authorization/header 脱敏、localhost 默认与显式远端信任。
- tools 先行；resources/prompts 分独立协议能力接入，不伪装成 tools。
- OAuth 在 Streamable HTTP 稳定后单独设计，不与基础 transport 一次交付。

官方规范：[MCP Streamable HTTP transport](https://modelcontextprotocol.io/specification/draft/basic/transports)。

### 7.4 IDE 验证

只做 VS Code 最小扩展，不同时启动 Desktop：

- 选择 workspace、创建/恢复 thread、发送消息。
- 流式渲染 token/tool/approval/diff。
- 变更文件定位、打开 diff、interrupt/steer。
- 全部通过 HTTP/SSE/SDK，不新增 IDE 专用 daemon 后门。

IDE 原型的目的，是验证 runtime API 是否足够稳定；达不到稳定性就先修 API，不复制一套状态逻辑到扩展。

### 7.5 验收

- stream-json golden schema 在版本升级测试中兼容。
- 断线后按 cursor 恢复，无重复终态、无事件缺口。
- SDK 示例可在 CI 中创建 thread、启动 turn、等待完成、读取 receipt。
- MCP Streamable HTTP 通过官方兼容 server 的 tools/list + tools/call + reconnect 测试。
- VS Code 原型能完成“发送任务 → 审批 → 查看 diff → 中断/续跑”的闭环。

---

## 8. R5：产品化、配置统一与发布工程

### 8.1 配置服务统一

- 提取共享 `ConfigurationService`，CLI `configure` 与 TUI `/config` 共用 provider preset、URL 校验、凭据迁移、模型探测和保存逻辑。
- 配置变更先 validate/doctor，再原子提交；失败不得破坏旧活动 route。
- 展示最终配置来源层级，但永不显示密钥正文。

### 8.2 TUI 收口

- 只继续拆分有明确所有权和测试收益的状态：session lifecycle、config flow、approval orchestration。
- 禁止以“app.py 行数”作为单独验收；用新增命令改动范围、回归率和测试隔离度衡量。
- 完成长任务视图：当前目标、活跃 worker、成本、等待审批、失败原因一屏可见。

### 8.3 安装、升级与诊断

- wheel、Docker、Windows 安装包/便携方式建立同一版本来源。
- runtime/session/routes/policy 迁移建立 upgrade/downgrade fixture；不支持降级时给出明确阻断。
- `coderook doctor` 汇总环境、provider、sandbox、工具链、数据库一致性、磁盘空间和端口状态。
- 日志与诊断包默认脱敏，用户确认后才能导出。

### 8.4 文档与发布

- README 只陈述通过 release scorecard 证明的能力。
- USER_GUIDE 按平台标出 sandbox 差异。
- 每个候选版本发布已知问题、benchmark commit、模型版本与迁移说明。
- 版本推进建议：R0–R2 达标为 `0.2.0-beta`；R4–R5 达标且连续两个候选版本无 P0 回归后再评估 `1.0`。

---

## 9. 改造前技术债映射

下表保留计划制定时的输入及其阶段归属，不表示当前仍未实现；现状以
`PRODUCTION_GAP_MATRIX.md` 的“实现状态”和“尚未取得的发布证据”两列为准。

| 当前问题 | 处理阶段 |
|---|---|
| Windows 无真实沙箱 | R1 |
| 双真源漂移风险 | R2 |
| ask_user 可无限等待 | R2 |
| TUI 图片入口缺失 | R3 |
| Python/TS 有限诊断 | R3 |
| Web Search 后端单一 | R3 |
| Diff 不能逐 hunk | R1 |
| 成本未进入 durable receipt | R2 |
| persistent shell 与后台任务分裂 | R2 |
| family 内部分派绕过完整管线 | R1 |
| TUI app 仍大 | R5，仅按收益拆分 |
| 两套配置向导 | R5 |
| token 粗估 | R3 后评估，不阻塞 Beta |
| HTTP/MCP 能力有限 | R4 |
| Artifact GC 无用户入口 | R2 |
| ProviderDoctor/价格长尾 | R3/R5 |
| 缺少真实任务成功率证据 | R0 |
| 缺少稳定机器接口和 IDE 验证 | R4 |

---

## 10. 横切测试矩阵

每个阶段除完整 CI gate 外，还必须覆盖：

| 维度 | 最低矩阵 |
|---|---|
| OS | Windows 11、Ubuntu LTS、macOS 当前支持版本 |
| Python | 3.12 锁定版本 |
| Git 工作区 | 普通仓库、monorepo 子目录、dirty worktree、非 Git 目录 |
| Provider | Anthropic Messages、OpenAI Chat、OpenAI Responses 至少各一条契约测试 |
| 权限 | ask、auto-review、full-access、headless fail-fast/allow-list/deny |
| 恢复 | 正常关闭、daemon 强杀、客户端断线、turn interrupt、预算耗尽 |
| 路径安全 | symlink/junction、UNC、空格、Unicode、大小写差异、超长路径 |

安全相关测试不得只 mock planner；必须包含真实子进程和真实文件系统负例。真实模型 benchmark
与确定性运行时测试分开，不能用模型波动掩盖运行时回归。

---

## 11. 每阶段交付纪律

每个阶段按以下顺序完成：

1. 写 ADR/威胁模型/协议变更说明。
2. 先增加失败测试或 benchmark case。
3. 实现最小闭环，不夹带下一阶段功能。
4. 跑完整 CI gate和该阶段专项矩阵。
5. 更新 `FUNCTIONAL_ARCHITECTURE.md`、用户文档与 release scorecard。
6. 独立提交；涉及 bus 模型时同步 `WIRE_PROTOCOL.md`。

完整门禁：

```bash
uv run ruff check .
uv run python scripts/check_brand.py
uv run mypy src
uv run mypy --platform linux src
uv run pytest -q
uv run python scripts/gen_protocol_doc.py --check
uv build
uv run python scripts/smoke_wheel.py dist
```

---

## 12. 停止条件与取舍规则

- Benchmark 没有改善的功能不进入默认路径；最多保留为实验开关。
- Windows 原型不能证明文件/注册表/网络边界时，不继续用“sandbox”命名，只保留 degraded 模式。
- 常驻 LSP、Desktop、OAuth、插件市场不得抢占 R0–R2 资源。
- 新协议必须先有版本化 schema 和兼容测试，再接第二个客户端。
- 任何安全门禁失败都阻止发布；文档、TUI 和 receipt 必须诚实显示降级状态。
- 连续两个阶段的新增复杂度使 benchmark 成功率下降超过 5% 时，暂停扩展并先偿还运行时复杂度。

---

## 13. 最终验收场景

候选版本必须完整演示并自动留证：

> 在一个带未提交用户改动的真实 monorepo 子目录中恢复历史 session；Agent 读取
> AGENTS.md，搜索并定位跨文件缺陷，生成可逐 hunk 审阅的修改；用户只接受部分 hunk；
> Agent 在平台可用的真实 sandbox 中运行验证，期间切换模型并触发一次 daemon 强杀；重启后
> 从 durable cursor 继续，完成测试并输出包含 route、成本、审批、文件、checkpoint、验证结果的
> TurnReceipt；同一任务可通过 stream-json 和 VS Code 原型观察，且工作区外没有任何写入。

该场景通过，且 §2.1 的量化门禁全部满足，CodeRook 才从“功能合格”升级为“效果与工程均合格”。
