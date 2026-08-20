# CodeRook 开源级补全计划

**状态**：执行基线 1.0

**制定日期**：2026-08-20

**适用基线**：CodeRook 0.1.0，commit `1a93b3bc971ca233bd345835b2a4e10bac9a5a71` 及当前工作树

**目标**：把 CodeRook 从“功能完整但证据不足的个人工程”推进为“陌生用户能够安装、理解、验证、贡献和安全使用的开源 Coding Agent”。

本计划是后续补全工作的统一执行清单。已有 R0-R5 生产改造不推倒重来；
`PRODUCTION_GAP_MATRIX.md` 继续记录底层能力证据，`RELEASE_SCORECARD.md` 继续决定是否发布，
本文负责补齐求职表达、Coding Agent 效果、开源治理、分发与社区采用之间的断层。

---

## 1. 调研结论

### 1.1 中文求职市场如何判断 Agent 项目

公开可访问的牛客文章与面经呈现出高度一致的判断标准：

1. **先看任务与责任边界**：项目解决了什么问题、团队规模、自身独立负责什么，不能把框架能力写成个人产出。
2. **再看自主决策链**：模型如何选择工具、状态如何流转、何时重试/降级/询问用户，而不是只写“接入大模型”。
3. **必须有工程闭环**：会话、日志、错误处理、权限、部署、并发、可观测、成本和回退策略决定它是工程还是 Demo。
4. **必须有量化证据**：任务成功率、P95 延迟、成本、恢复率、错误率、测试规模和实际使用量至少占一类。
5. **表述必须能被追问**：技术名词后面要接设计选择、替代方案、失败案例和验证结果。

因此，简历项目描述采用以下固定结构：

> 场景/问题 + 本人职责 + 自研机制 + 关键取舍 + 可复现实证

不采用以下结构：

> 使用 LangChain/LangGraph/MCP/RAG 搭建智能体，实现多 Agent 协作

后者没有说明模型为何能正确完成任务，也无法证明个人贡献。

### 1.2 Coding Agent 与通用 Agent 的能力差异

通用 Agent 项目通常包含：模型接入、Prompt、工具调用、工作流、多 Agent、RAG、短期/长期记忆、
MCP、人工确认、评测、监控、后端 API、部署、成本与安全。

Coding Agent 的核心顺序不同：

1. 仓库理解：文件搜索、符号关系、repo map、规则文件和动态 working set。
2. 安全修改：精确编辑、patch、冲突检测、diff、checkpoint 和回滚。
3. 执行验证：shell、lint、test、LSP、失败反馈与有限修复循环。
4. 长程运行：计划、steering、恢复、上下文压缩、成本与轨迹。
5. 权限边界：工作区、审批、沙箱、网络和凭据保护。
6. 可扩展与自动化：MCP、skills、hooks、headless/SDK/IDE。
7. 可复现效果：真实仓库任务与公开 benchmark，而不只是组件单测。

CodeRook 不应为了贴合通用 Agent 简历而把向量 RAG 设为核心依赖。更有价值的路线是先完成
Git-aware repo map、符号检索和可解释 working set；外部知识 RAG 以后通过 MCP/Skill 插件提供。

### 1.3 主流开源 Coding Agent 给出的基线

| 项目 | 可借鉴的公开能力 | 对 CodeRook 的要求 |
|---|---|---|
| OpenHands | Docker/远端沙箱、轨迹、预算、SDK、可扩展 runtime | 沙箱状态必须诚实；运行与评测环境可复现；轨迹和成本可审计 |
| SWE-agent | 面向代码仓库的 Agent-Computer Interface、完整 trajectory、Docker 任务环境 | 工具接口要为模型优化；每次任务保留可复现轨迹 |
| Aider | repo map、Git 集成、自动 lint/test、多编辑格式、公开 Polyglot benchmark | 增加仓库级代码理解；用公开任务集而非自建题自证 |
| Cline | IDE 审批、浏览器/终端、MCP、逐步 checkpoint、compare/restore | TUI/VS Code 必须让变更、权限和回滚可见、可操作 |
| goose | 多 provider、MCP/skills/recipes/subagents、桌面/CLI/API、开放标准与社区治理 | 保持模型无关和扩展协议；提供样例、贡献路径与稳定发行 |

这些项目不是要求 CodeRook 复制所有产品面。CodeRook 的差异化应保持为：

- daemon + 多客户端的 durable 本地运行时；
- typed protocol-first 的 JSON-RPC/SSE 契约；
- fail-closed 权限、安全编辑与恢复；
- 可审计的多 Agent/workflow/receipt；
- TUI-first，同时保留稳定机器接口。

---

## 2. 当前项目结论

### 2.1 已达到开源 Agent 水平的底座

| 领域 | 当前证据 |
|---|---|
| 运行时 | daemon/client 分离、异步 Agent loop、durable thread/turn、断线重连 |
| 工具与编辑 | File/Git/Bash/Run、事务 patch、hash 冲突、checkpoint/rewind、LSP 诊断 |
| 安全 | loopback token、工作区边界、六层权限、Linux/macOS 强制后端、Windows 明确降级 |
| 长程任务 | 上下文压缩、steer/interrupt、后台任务、成本/usage、故障恢复与 receipt |
| 多 Agent | task board、subagent、fleet、worktree、声明式 workflow、写冲突约束 |
| 扩展 | MCP、Skills、Hooks、Python SDK、HTTP/SSE、VS Code 候选 |
| 自动验证 | 1,000+ 本地测试、Ruff、双平台 Mypy、协议生成、wheel smoke、50 任务 harness |

这些内容足以作为简历里的“系统设计与工程实现”，无需再堆一层 Agent 框架。

### 2.2 当前不具备公开发布资格的缺口

| 优先级 | 缺口 | 当前判断 |
|---|---|---|
| P0 | 真实模型效果 | 50 任务执行器已存在，但没有 pass@1、成本、波动与失败分布 |
| P0 | 公开 benchmark | 没有 Aider Polyglot 或 SWE-bench 的适配与可复现报告 |
| P0 | 三平台可信交付 | 最新远端 CI 未绿；distribution、强杀、安全矩阵仍缺真实报告 |
| P1 | 首次安装体验 | 开发者安装路径存在，PyPI/GHCR/portable 的干净机体验与时长没有证明 |
| P1 | 供应链安全 | secret/dependency/CodeQL workflow 已落地；SBOM、provenance、签名和远端绿灯仍缺 |
| P1 | 外部兼容 | MCP 官方 SDK 三 transport 报告已产生；VSIX Extension Host 远端冒烟报告尚未产生 |
| P2 | 采用与社区 | 版本变更、贡献与支持路径已落地；真实录屏、首批 good first issue 和外部用户反馈仍缺 |

### 2.3 范围边界

公开 Beta 前不做以下扩张：

- 不引入 LangGraph/CrewAI 来重写已有 loop/workflow。
- 不把外部知识 RAG、向量数据库或 Web UI 设为发布阻塞项。
- 不承诺 Windows 具备不存在的文件系统沙箱；保持 degraded + ASK 即为正确行为。
- 不建设插件市场、多租户 SaaS、云同步或完整桌面应用。
- 不为追求功能数量复制 Cline/OpenHands 的每个入口。

---

## 3. 完成定义

只有同时满足以下条件，才能把本计划标记为完成并发布 `0.2.0-beta`：

### 3.1 用户可用

- 陌生用户从 README 开始，在 Windows/Ubuntu/macOS 任一支持平台 10 分钟内完成安装、配置和首个只读任务。
- 启动 TUI 不强制弹出 API 配置；未配置时显示可理解的空状态和 `/config` 指引。
- wheel、容器和 Windows portable 均有干净环境 smoke 报告；至少一种方式可直接从 GitHub Release 获取。
- 配置、模型、权限、sandbox 状态和诊断命令在用户文档中一致。

### 3.2 Coding Agent 效果

- 自建 50 任务候选集：总体 pass@1 ≥80%，多文件 ≥75%，只读 ≥90%。
- 至少接入一个公开 benchmark；报告固定 commit、模型、route、预算、容器、成功率、成本和失败分类。
- repo map 在中型仓库上可增量构建，并能以固定 token 预算提供符号级上下文。
- 至少两个 wire format/代表性模型各重复两次，报告波动，不挑最好一次宣传。

### 3.3 安全与可靠性

- 三平台安全负例 100% 通过；不支持的能力必须 fail closed 或 ASK。
- 三平台 daemon 强杀恢复各 100 次，成功率 ≥95%，孤儿 tool call 为 0。
- 自动检查仓库秘密、依赖漏洞和代码安全；发布产物包含 SBOM 与 provenance。
- 任何文档、trace、benchmark artifact 和测试 fixture 都不包含真实密钥。

### 3.4 开源工程

- 存在有效 LICENSE、CONTRIBUTING、SECURITY、CODE_OF_CONDUCT、CHANGELOG、支持边界和 Issue/PR 模板。
- `pyproject.toml` 与 GitHub 仓库元数据完整，README 有安装、架构、演示、限制、贡献和 benchmark 链接。
- 主分支保护或 ruleset 要求 CI，通过可复现 release workflow 生成版本产物。
- 至少提供 3 个从零可运行样例：本地修复、只读审查、MCP/Skill 扩展。

### 3.5 简历证据

- 每个简历数字都可定位到公开报告或 CI artifact。
- 能展示一次真实失败及改进过程，而不只展示成功案例。
- 能清楚区分本人自研、第三方依赖、模型能力和测试环境。

---

## 4. 分阶段执行计划

状态约定：`DONE` 已有仓库证据；`PARTIAL` 已实现但缺验收；`TODO` 尚未实现；
`EXTERNAL` 需要真实模型、远端平台或发布账号。

### OS0：事实校准与开源治理（P0）

| ID | 状态 | 工作项 | 验收证据 |
|---|---|---|---|
| OS0-01 | DONE | 增加 MIT LICENSE，并校准 README 的 License 链接 | `LICENSE` 与 README 本地链接门禁通过；GitHub 识别待推送复验 |
| OS0-02 | DONE | 增加 CONTRIBUTING、SECURITY、CODE_OF_CONDUCT、SUPPORT、CHANGELOG | 贡献、私下安全报告、支持边界、行为与版本记录均已落盘 |
| OS0-03 | DONE | 增加 bug/feature/config Issue 表单和 PR 模板 | 表单要求版本、平台、sandbox、脱敏诊断与验收条件 |
| OS0-04 | DONE | 完善 `pyproject.toml` 包元数据与项目 URL | `check_public_repo.py` 校验字段；wheel METADATA 随完整 gate 复验 |
| OS0-05 | DONE | 清理历史文档过时结论，建立文档状态/更新时间/权威来源 | `docs/README.md` 建立权威/专题/历史分层，关键旧文档增加快照提示 |
| OS0-06 | PARTIAL | 删除 fixture/cache/本地产物污染，增加 secret/history 扫描 | 跟踪产物门禁、Dependabot、CodeQL、dependency review 与 gitleaks workflow 已配置；远端报告待产出 |

阶段出口：公开仓库不再出现断链许可证、相互矛盾的启动说明或无法报告安全问题的状态。

### OS1：安装、配置与首个任务（P0）

| ID | 状态 | 工作项 | 验收证据 |
|---|---|---|---|
| OS1-01 | DONE | 统一无配置启动、`/config`、`coderook configure` 与 provider doctor | TUI 不调用强制向导；配置/TUI 相关 27 项测试通过 |
| OS1-02 | PARTIAL | 建立 10 分钟 quickstart 与脚本化 first-run smoke | 隔离 HOME、零凭据 wheel/Core/ping 最新本机 8.73 秒通过；三平台干净报告待 CI |
| OS1-03 | PARTIAL | wheel、Docker、Windows portable 发行流水线 | 可复用零凭据 smoke 已在本机安装态 6.0 秒通过，并接入容器和 portable job，覆盖配置状态、TUI help、真实 Core/ping；远端产物报告待 CI |
| OS1-04 | PARTIAL | 已提供升级、配置、数据备份与回滚手册 | 首个公开版本后补“上一版本 → 当前版本 → 回滚”fixture |
| OS1-05 | DONE | 增加 `examples/`：只读审查、自动修复、MCP 扩展 | 命令契约离线测试；MCP 示例由真实 stdio 客户端握手与调用 |
| OS1-06 | DONE | 用真实 Textual 控件和确定性正式事件制作可复现 TUI 截图，更新 README 首屏 | `capture_tui_demo.py` 无 daemon/模型生成截图；SVG 内容测试与公开仓库门禁覆盖 |

阶段出口：一个不了解代码库的人能够安装、配置模型、完成任务、退出并恢复会话。

### OS2：仓库理解与编码成功率（P0）

| ID | 状态 | 工作项 | 验收证据 |
|---|---|---|---|
| OS2-01 | DONE | Git-aware repo map：文件、语言、关键符号、签名、依赖与引用摘要 | 真实 Git fixture 覆盖 Python/TypeScript、ignore、敏感路径排除 |
| OS2-02 | DONE | daemon 内增量缓存与失效：commit/worktree/hash 感知 | 单文件修改仅重建 1 项，cache hit/parsed/hash 均有断言 |
| OS2-03 | DONE | repo map 纳入 context assembly，固定预算选择相关符号 | 12K 字符硬上限（约 3K token）；选择理由进入 typed event/receipt |
| OS2-04 | DONE | `Repository.symbols/references`，语法索引外回退文本匹配 | Python/TypeScript 定义、调用与结构化工具协议测试 |
| OS2-05 | DONE | 编辑后 Python/TS 诊断 + `Run.tests/verifiers` 结构化验证 + 有限修复守卫 | typed completed/failed 事件进入 receipt；相同语义重复 3 次终止，另受 max_steps 限制 |
| OS2-06 | DONE | `coderook review` 只读 preset 与结构化输出契约 | allow-list 无写工具；结果要求位置、P0-P3、证据、风险和验证 |

实现原则：先用确定性解析与轻量缓存；embedding 只能作为可选 provider，不能成为基础安装或隐私前提。

阶段出口：CodeRook 能在未手工喂文件的中型仓库中定位跨文件关系，并把检索选择解释清楚。

### OS3：安全、恢复与可观测证据（P0）

| ID | 状态 | 工作项 | 验收证据 |
|---|---|---|---|
| OS3-01 | PARTIAL | 恢复 Ubuntu/Windows/macOS CI 全绿并连续验证 | 默认分支至少 3 次连续全绿 |
| OS3-02 | EXTERNAL | 三平台运行 sandbox 负例矩阵 | artifact 记录 backend、enforced/degraded 与每项结果 |
| OS3-03 | EXTERNAL | 三平台各运行 100 次 daemon 强杀恢复 | 每平台 ≥95%，孤儿 tool call=0 |
| OS3-04 | PARTIAL | 建立 ProcessSupervisor 资源 P95 基线 | benchmark 报告已投影 wall/CPU/RSS/进程数和完整性；真实三平台报告待产出 |
| OS3-05 | PARTIAL | 增加 CodeQL、dependency review、Dependabot 和 secret scan | `security.yml` 与 `dependabot.yml` 已配置；首次远端结果待产出 |
| OS3-06 | DONE | 明确模型、MCP、Skill、Hook、shell、网络、workspace 与供应链 threat model | `THREAT_MODEL.md` 与 SECURITY 互链，含支持/降级/拒绝、非目标和响应流程 |

阶段出口：安全能力以“支持/降级/拒绝”三态表达，所有可靠性数字来自真实进程和真实 OS。

### OS4：公开评测与效果优化（P0）

| ID | 状态 | 工作项 | 验收证据 |
|---|---|---|---|
| OS4-01 | DONE | 固化 50 任务真实模型候选协议 | 落盘前强制完整 commit、route/model/wire、配置/task/fixture/budget/candidate SHA-256；逐任务公开允许工具与四类预算，比较器拒绝合同漂移 |
| OS4-02 | EXTERNAL | 跑两个 wire format × 两次候选集 | 四原始报告 + 唯一 aggregate gate 的代码/工作流已完成；真实模型凭据触发后才产生 pass@1、分类阈值、成本、波动和不稳定任务报告 |
| OS4-03 | PARTIAL | 适配 Aider Polyglot benchmark，使用隔离容器执行 | 官方目录/prompt/test loader、固定 commit、容器入口和统一报告已实现；真实固定切片待跑 |
| OS4-04 | PARTIAL | 增加 SWE-bench Lite/Verified 小规模适配 smoke | 标准三字段 JSONL、基线校验、含新增文件 patch 与官方 harness 命令已实现；官方 Docker 判分 artifact 待产出 |
| OS4-05 | DONE | 建立回归比较器：基线/候选、显著退化、失败聚类 | `compare_benchmark_reports.py` 输出 JSON/Markdown，门禁任务回退、安全负例、效果、P95 成本/耗时和任务集漂移 |
| OS4-06 | EXTERNAL | 按检索、编辑、验证、权限、预算、模型错误分类优化 | 六域 backlog 与 experiment recorder 已接入 release workflow，强制绑定前后报告 SHA-256、完整 commit 和同一评测合同；真实 OS4-02 报告产生后才能实施并接受优化 |

公开报告不得只报 pass@1；同时报告有效样本、超时、格式错误、成本、耗时、配置和失败案例。
SWE-bench 完整集资源消耗很大，Beta 门禁只要求标准兼容与固定小样本；不得把非标准子集冒充官方榜单成绩。

阶段出口：CodeRook 的效果可以被第三方在固定 commit 上复现，简历数字有公开出处。

### OS5：扩展、TUI 与 IDE 交付（P1）

| ID | 状态 | 工作项 | 验收证据 |
|---|---|---|---|
| OS5-01 | DONE | 对固定官方 MCP Python SDK 2.0 server 完成 stdio/legacy SSE/Streamable HTTP 兼容矩阵 | Windows dated JSON/Markdown 绑定 commit c47ae23，三种 transport 的 tools/resources/prompts/cancel/reconnect 全通过；Ubuntu workflow 持续复验 |
| OS5-02 | DONE | 提供可安装 focused-fix Skill、敏感文件阻断 Hook 与 MCP stdio 示例及安全说明 | Skill 经受管安装/digest/渲染 smoke；Hook 经真实配置加载与子进程阻断/放行；MCP 完成握手与调用 |
| OS5-03 | PARTIAL | VSIX 在真实 Extension Host 中连接 daemon、审批、diff、恢复 | Xvfb + 隔离真实 daemon runner 已覆盖激活、命令、新建/恢复 thread 与 diff，并上传 commit-bound JSON；远端报告及审批 UI 录像/截图待产出 |
| OS5-04 | DONE | TUI 首次使用、连接失败、无模型、模型失败、degraded sandbox 状态均给出非阻塞恢复建议 | 真实 Textual 截图 + app/connection/render 交互测试覆盖，重连提示去重 |
| OS5-05 | DONE | TUI 统一展示 plan、repository context、working set、diff、验证结果、receipt context 与恢复点 | 正式事件渲染与 Turn Inspector 测试；用户无需读取原始 JSON |
| OS5-06 | DONE | 公布 HTTP/SSE、Python SDK、stream-json 的兼容、错误与版本策略 | capabilities/响应头/模型默认值/SDK 契约测试；两 minor 且不少于 90 天的弃用窗口写入文档 |

阶段出口：扩展作者和 IDE 用户都有受支持的最短路径，TUI 仍是首先验收的产品面。

### OS6：发行、安全供应链与社区采用（P1）

| ID | 状态 | 工作项 | 验收证据 |
|---|---|---|---|
| OS6-01 | DONE | 建立评分卡 GO 才可通过的 tag release workflow 与 SemVer/PEP 440 规则 | 脚本校验 tag、Python/VSIX 版本、协议清单、Changelog heading/link；workflow 先跑完整与三平台 distribution gate |
| OS6-02 | PARTIAL | 为 wheel/sdist/portable/VSIX/容器生成 SPDX SBOM、manifest 与 checksums | 生成脚本和 workflow 合同已完成；首次真实 Release 页面资产待 OS6-04 |
| OS6-03 | PARTIAL | 使用 GitHub OIDC、actions/attest 与 Cosign keyless 签名下载资产和容器 digest | 无长期发布密钥；本地合同已完成，远端 attestation/bundle 验证待首次 tag |
| OS6-04 | EXTERNAL | 发布 PyPI 包、GHCR 镜像和 GitHub Release 候选 | 干净机从公开地址安装成功 |
| OS6-05 | PARTIAL | 以稳定汇总 job 固定 CI/Security 必需检查，提交 CODEOWNERS、Dependabot 与 main ruleset 配置/审计合同 | 仓库合同与离线检查已完成；GitHub active ruleset API 证据仍属外部状态，未启用前不宣称 main 已保护 |
| OS6-06 | DONE | 建立 outcome-based Roadmap、4 个 READY contributor task、认领模板、支持升级路径和单维护者边界 | 陌生贡献者可按 ID 选择、认领、验证一个小任务；文档明确总线因子与权限边界 |

阶段出口：`0.2.0-beta` 是可验证的公开发行，而不是仓库中的版本字符串。

### OS7：简历、项目讲解与证据封装（P1）

| ID | 状态 | 工作项 | 验收证据 |
|---|---|---|---|
| OS7-01 | DONE | 生成一页 `PROJECT_CASE_STUDY.md` | 问题、架构、取舍、指标、本人/第三方职责与 NO-GO 边界完整 |
| OS7-02 | DONE | 固化 3 分钟与 10 分钟项目讲解、演示和追问提纲 | 每个主题可跳转架构、代码域、评分卡或复盘 |
| OS7-03 | DONE | 建立 `RESUME_EVIDENCE.md` 指标账本 | 稳定事实、dated checkpoint、条件模板、禁止宣称和更新流程分层 |
| OS7-04 | DONE | 发布 CI #31 失败复盘与 TUI 重构优化复盘 | 同时记录根因、纠正措施、未验证项、目标偏差与不能推导的收益 |

阶段出口：仓库首页、简历和面试回答使用同一组事实，不互相夸大。

---

## 5. 执行顺序与依赖

```text
OS0 开源治理
  ├─> OS1 安装与首用
  ├─> OS2 repo map / coding quality ─> OS4 公开评测
  └─> OS3 安全与恢复 ───────────────┤
                                      ├─> OS6 公开发行 ─> OS7 简历证据
OS5 MCP/TUI/VSIX ────────────────────┘
```

强制顺序：

1. 先完成 OS0，避免继续在错误文档和不完整开源元数据上累积工作。
2. OS2 与 OS3 可交错推进；OS4 必须使用 OS2 后的候选能力重新建立基线。
3. OS4 指标不达标时，只按失败分类回流 OS2/OS3，不凭感觉新增功能。
4. OS6 发布必须被 OS1、OS3、OS4、OS5 的阶段出口共同阻塞。
5. OS7 只能引用已经生成的报告，不能预填未来成绩。

每个工作项完成时必须同时：实现、增加适度测试、更新用户文档、记录验收命令和证据链接。
只有代码而无外部报告的项保持 `PARTIAL` 或 `EXTERNAL`。

---

## 6. 简历写法

### 6.1 当前阶段可安全使用的描述

**CodeRook｜本地优先的 Durable Multi-Agent Coding Runtime｜个人项目**

- 设计并实现 daemon/client 双进程 Coding Agent，基于 Pydantic 判别联合构建类型化
  JSON-RPC/NDJSON 与 HTTP/SSE 协议，支持多轮会话、断线重连、上下文压缩和可审计 Turn Receipt。
- 构建 File/Git/Bash/Run 工具管线与六层权限决策，实现工作区边界、事务 PatchPlan、逐 hunk 审批、
  checkpoint/rewind；Linux/macOS 使用 OS 沙箱包装，Windows 无强制后端时 fail-closed 降级为人工审批。
- 实现 task/subagent/fleet/worktree/workflow 多 Agent 编排，使用写入声明、预算、租约与事件溯源账本约束
  并发修改和崩溃恢复，并通过 TUI、headless CLI、Python SDK 和 VS Code 原型暴露统一运行时。
- 建立 50 任务离线评测框架与 1,000+ 自动化测试门禁，覆盖协议、工具、安全、持久化、恢复、MCP 和分发；
  真实模型 pass@1 尚在候选评测中，不在简历中虚报生产成绩。

### 6.2 完成计划后替换为量化描述

最后一条替换为真实数字，不保留占位符：

> 在固定模型/route/预算下完成 N 个自建真实仓库任务与 M 个公开 benchmark 任务，pass@1 为 X%，
> 多文件任务为 Y%，单任务 P50/P95 成本为 A/B；三平台 100 次 daemon 强杀恢复率为 Z%，
> 所有配置、轨迹和失败报告公开可复现。

### 6.3 面试重点

优先准备以下问题：

1. 为什么采用 daemon + client，而不是单进程 CLI？
2. transcript 文件账本与 SQLite 投影为何是双真源，如何 reconcile？
3. 模型产生一个 shell/patch 请求后，权限、hook、sandbox、执行和 receipt 如何串起来？
4. Windows 没有可靠文件系统 sandbox 时为什么选择 ASK，而不是假装自动安全？
5. repo map 如何控制 token、增量失效并影响任务成功率？
6. 真实 benchmark 失败主要来自检索、编辑、验证还是模型，如何证明？
7. 多 Agent 在什么任务上提升效果，在什么任务上只增加成本和冲突？

---

## 7. 资料来源与使用边界

中文社区内容用于总结面试官关注点，不作为技术实现权威；技术能力以官方文档、论文和仓库为准。
小红书公开页面在本次检索中无法获得稳定、可核验的正文索引，因此没有用不可访问的笔记凑结论。

### 求职与面试

- 牛客：[Agent 项目简历怎么写](https://www.nowcoder.com/discuss/832013460337672192)
- 牛客：[RAG / Agent 项目面试官想听什么](https://www.nowcoder.com/discuss/893051252416737280)
- 牛客：[面试官视角拆 Agent 项目简历](https://www.nowcoder.com/discuss/898495069127200768)
- 牛客：[Agent 开发简历能力结构](https://www.nowcoder.com/discuss/906102877850980352)
- 招聘需求参考：[美团 Agent 全栈岗位](https://www.zhaopin.com/jobdetail/CC383625320J40876532909.htm)
- 能力模型汇总：[AI Agent 招聘需求与能力模型](https://todayforai.com/zh/guides/20260722-guide-agent-engineer-00-recruitment-requirements-report)

### 开源 Coding Agent 与评测

- OpenHands：[SDK](https://docs.openhands.dev/sdk/index)、[Sandbox](https://docs.openhands.dev/openhands/usage/sandboxes/process)
- SWE-agent：[论文](https://arxiv.org/abs/2405.15793)、[仓库](https://github.com/SWE-agent/SWE-agent)
- Aider：[功能文档](https://aider.chat/docs/)、[Benchmark Harness](https://github.com/Aider-AI/aider/blob/main/benchmark/README.md)
- Cline：[仓库](https://github.com/cline/cline)、[MCP 文档](https://github.com/cline/cline/blob/main/docs/mcp/mcp-overview.mdx)
- goose：[官方文档](https://block.github.io/goose/)
- SWE-bench：[官方 Harness](https://github.com/SWE-bench/SWE-bench/blob/main/README.md)

---

## 8. Goal 完成规则

本计划作为后续 Goal 的事实来源。执行中遵守以下规则：

- 默认选择第一个未阻塞的 P0 `TODO`，不以新增功能绕过验收。
- 完成项必须把状态改为 `DONE`，并在 `RELEASE_SCORECARD.md` 记录证据。
- 真实模型费用、GitHub/PyPI/Marketplace 发布或平台 secret 属于外部动作，执行前使用用户明确授权的账号配置；
  未取得外部结果时保持 `EXTERNAL`，不得伪造通过。
- 每次推送前运行仓库规定的完整 CI gate；失败即阻止推送。
- 所有 P0/P1、外部发布门禁和公开安装 smoke 完成前，Goal 不标记为 complete。
