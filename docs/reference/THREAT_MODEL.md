# CodeRook 威胁模型

状态：Current

最近更新：2026-08-20

本文档描述 CodeRook 当前能够强制的边界、明确降级的能力和仍由用户承担的风险。它不是“绝对安全”声明；
发布资格仍以[发布评分卡](../status/RELEASE_SCORECARD.md)为准，漏洞披露流程见[安全策略](../../.github/SECURITY.md)。

## 1. 范围与安全目标

CodeRook 是在单个用户本机运行的 Coding Agent。核心安全目标是：

1. 未经权限层允许，不把模型输出直接升级为文件写入、shell、网络或外部系统操作；
2. 直接文件工具不能越过选定工作区，不能通过 `..`、绝对路径或 symlink 逃逸；
3. 凭据不进入项目配置、模型提示、普通事件、公开报告或诊断包；
4. daemon 崩溃、客户端断线或工具取消后，durable ledger 保持可检测、可恢复、可审计；
5. 平台无法提供真实强制能力时明确标记 degraded 或拒绝执行，不把降级冒充隔离。

保护资产包括源码与 Git 历史、用户级 CodeRook 状态、API/MCP 凭据、会话与 memory、运行机器上的其他文件、
本地/远端服务访问权，以及 benchmark 和 receipt 的证据完整性。

## 2. 信任假设与边界

| 组件/输入 | 默认信任 | 边界 |
|---|---|---|
| 用户与当前 OS 账户 | 可信控制者 | 用户可以显式批准高风险操作；同账户其他进程不在隔离范围内 |
| 模型输出与 reasoning | 不可信 | 只能通过 ToolRegistry、authority、permission、workspace/sandbox 管线产生动作 |
| 仓库文件、Issue 文本、网页和工具输出 | 不可信内容 | 可能包含 prompt injection；内容本身不授予权限 |
| 工作区 | 未信任，除非用户显式 grant trust | project Hook 和自动化能力受 trust 状态约束 |
| MCP server | 不可信外部执行方 | 动态工具保守声明为 EXTERNAL、延迟激活、有审批与输出边界 |
| Skill | 指令，不是安全策略 | 受 provenance/digest 校验；Skill 不能绕过工具权限 |
| Hook | 本地可执行代码 | project Hook 只在可信工作区运行；有超时、输出上限、审计和 failure policy |
| shell 子进程 | 高风险 | ProcessSupervisor 只管生命周期；真实文件/网络隔离取决于 OS sandbox backend |
| IPC/HTTP 客户端 | 本机调用方 | IPC 首帧 token；HTTP Bearer；非 loopback HTTP 必须配置 token |
| GitHub Actions 与依赖 | 供应链输入 | 最小 workflow 权限、固定/主版本 action、依赖审查、CodeQL、secret scan |

模型、Skill 或仓库中的文字即使声称“已获授权”，也不能改变 authority 快照、工具 capability 或审批结果。

## 3. 攻击面与当前控制

| ID | 威胁与可能影响 | 当前控制 | 状态与剩余风险 |
|---|---|---|---|
| TM-01 | Prompt injection 诱导读取秘密、改代码或执行命令 | 工具 capability、PLAN 只读、权限六层决策、headless fail-fast/allow-list；repo map 默认排除 `.env*`、`.coderook/`、credential/token/runtime 文件 | **支持边界**：不依赖模型自律；不保证识别所有恶意文本，用户批准仍可能授权危险动作 |
| TM-02 | 路径穿越、绝对路径或 symlink 逃出 workspace | `WorkspaceBoundary.resolve()` 对所有直接文件工具做 resolved-path containment；patch/edit 事务、hash 冲突和 checkpoint | **支持**；shell 路径不由此边界强制，见 TM-03 |
| TM-03 | shell 命令修改工作区外文件、派生孤儿进程或逃逸 | deny patterns、outside-cwd 检测、审批、ProcessSupervisor 杀进程树；Linux bwrap/macOS Seatbelt 可包装 shell/Run/background/fleet | Linux/macOS 后端可用时 **enforced**；Windows 或缺失后端 **degraded + ASK**。Job Object 不是文件系统边界 |
| TM-04 | SSRF、重定向到内网、任意出站或数据外泄 | WebFetch 每跳校验公网地址、协议/大小/超时边界；sandbox 支持完全断网；域白名单无法强制时 fail closed | SSRF 路径 **支持**；按域正向放行 **拒绝**；用户显式允许全网后仍可能发生内容外泄 |
| TM-05 | API key 出现在仓库、prompt、trace、报告或日志 | keyring 优先、0600 credential fallback、项目 TOML 禁止 route secret、trace 键/模式脱敏、诊断包确认导出、secret scan | **支持但非 DLP**；模型若通过用户允许的 shell 读取任意秘密，无法保证识别所有格式 |
| TM-06 | 未授权本机进程调用 daemon/API | IPC token 独占创建、0600、首帧认证、常量时间比较、loopback peer；HTTP Bearer，非 loopback 空 token 启动失败 | **支持**；同一 OS 账户下能读取 token 的恶意进程不属于隔离目标 |
| TM-07 | MCP server 伪造 schema、返回恶意内容、执行外部副作用或耗尽资源 | 远端 HTTP 要求 HTTPS（loopback 例外）、token 仅从 env 注入、30s/64MB transport 边界、deferred discovery、8K/20K output spill；MCP 工具默认 EXTERNAL/ASK | **部分支持**；CodeRook 不验证第三方 server 的业务语义，批准调用即信任其外部行为 |
| TM-08 | Skill 安装后被篡改或用指令绕权 | preview/确认安装、来源/trust 元数据、排序内容 SHA-256、symlink 拒绝、调用前 digest mismatch fail closed | 受管 Skill **支持完整性**；unmanaged/legacy Skill 明示 untrusted，内容安全仍需人工审查 |
| TM-09 | Hook 执行任意本地代码、阻塞 daemon 或泄露上下文 | workspace trust gate、固定 argv、超时、输出上限、有界队列、ProcessSupervisor、审计事件、fail-open/closed 显式配置 | **高风险扩展**；可信 project/user Hook 与手工脚本等价，不提供语言级 sandbox |
| TM-10 | 崩溃导致 ledger 尾部损坏、孤儿 tool call 或 SQLite 投影漂移 | checksum chain、尾部恢复、SQLite 投影、runtime doctor/reconcile、强杀矩阵、turn 终态配对校验 | 仓库内控制 **支持**；三平台 300/300 属于绑定旧 commit 的历史证据，当前提交状态见评分卡 |
| TM-11 | 多 Agent 并发覆盖文件或未审查合并 | WriteClaim 静态冲突、resource claims、worktree 隔离、review gate、checkpoint hash 冲突 | **支持已声明范围**；错误或过宽的协调契约仍需 owner/reviewer 判断 |
| TM-12 | 依赖、workflow 或发布产物被供应链污染 | workflow 最小权限、Dependabot、可选 dependency review、CodeQL、Gitleaks、构建/smoke | **部分支持**；Actions 当前关闭且 main 未保护，首次 Release 的 SBOM、provenance 与签名尚未产生 |
| TM-13 | 超大/恶意工具输出耗尽内存或污染长期上下文 | transport/tool/frame 上限、输出蒸馏/截断/artifact spill、context compaction、MCP/Web 大小限制 | **支持有界处理**；允许的 artifact 仍占本地磁盘，由 GC 和磁盘 doctor 管理 |

## 4. 权限与沙箱判定

`RuntimeMode × AuthorityProfile × WorkspaceTrust × allowed_actions` 在 turn 启动时冻结。PLAN 只允许 READ；
ACT/OPERATE 仍要经过工具 action capability 与 permission policy。`ApprovalRequirement.NEVER` 仅用于实现声明的纯读工具；
未知工具和保守外部工具默认 ASK。

“允许”与“隔离”是两个不同结论：

- permission 决定当前动作是否获准；
- sandbox plan 说明获准动作是否被 OS 强制限制；
- `enforced=false` 或 `degraded_reason` 非空不能描述为 sandboxed；
- Windows 的 ProcessSupervisor/Job Object 只提供终止与资源记账，不提供文件系统或网络隔离。

## 5. 凭据与隐私数据流

LLM 请求会包含用户消息、选中的仓库摘要、已读取文件、工具结果与会话上下文。使用远端模型意味着这些内容会发送给
对应 provider；CodeRook 不改变 provider 的数据保留政策。MCP、Web 和 shell 只有在其工具实际获准并调用时接收参数。

用户不应把密钥写入 prompt、仓库文件、Skill、Hook 输出或 Issue。benchmark 报告只保存 route/model/wire、配置 hash、
token/成本和有界证据，不保存 credential。仓库地图不读取默认敏感状态，但显式 `File.read` 仍可在权限允许时读取工作区文件。

## 6. 明确非目标

当前版本不承诺：

- 抵御拥有同一 OS 账户、管理员/root 权限或能读取进程内存的本地攻击者；
- 多租户隔离、服务端托管隔离或恶意用户之间的权限分离；
- Windows AppContainer/受限令牌级文件系统 sandbox；
- 按 DNS 域强制的出站白名单；
- 自动判断第三方 MCP、Skill、Hook 或模型供应商本身可信；
- 检测所有 prompt injection、秘密格式、许可证问题或生成代码漏洞；
- 在用户明确批准高风险 shell/网络操作后继续阻止其预期副作用。

## 7. 安全事件响应

发现疑似越权或泄露时：

1. 停止 `coderook-core` 和相关后台任务，保留 run/session/trace 的只读副本；
2. 撤销或轮换可能暴露的 LLM、MCP、GitHub 等凭据；
3. 记录版本、commit、OS、sandbox backend、authority、工具调用和最小复现，先完成脱敏；
4. 通过 GitHub Private Vulnerability Reporting 私下提交，不在公开 Issue 粘贴 PoC 或秘密；
5. 修复应包含边界回归测试、受影响版本判断、必要的数据迁移/撤销步骤和安全公告；
6. 只有对应负例、完整 CI 和相关外部矩阵恢复通过后，才重新给出支持结论。

安全控制与实现不一致时，以更保守的运行时行为和[安全策略](../../.github/SECURITY.md)为准，并把文档差异视为缺陷处理。
