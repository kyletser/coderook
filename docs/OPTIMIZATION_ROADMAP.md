# CodeRook 优化路线图（2026 H2）

- 生成日期：2026-08-16
- 基准代码版本：`817cce1`（feat: expand LLM provider support, runtime service, and project docs）
- 时间跨度：约 14 周（3-6 个月），综合路线：体验快赢 → 生态兼容 + 感知能力 → 智能与经济性 → 安全与可信 → TUI 重构 + 管理面板 → 形态扩展
- 现状基准：`docs/FUNCTIONAL_ARCHITECTURE.md` v1.1 §20（2026-08-06）
- **进度基线（2026-08-17 更新）**：阶段 0、1、2 全部完成（阶段 2 收尾见 §5.6 复盘）；下一步从阶段 3（安全与可信，W3.1 沙箱为首）。各工作项状态见正文 ✅/⏸/⬜ 标注与 §5.6 复盘记录。
- 本文档**取代**以下历史差距分析（它们描述的缺口多数已修复或已过时）：
  - `docs/CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS.md`（2026-07-15，35-45% parity，历史快照）
  - `docs/CLAUDE_CODE_EXPERIENCE_PARITY.md`（2026-07-30，81/100，早于 08-04 R1-R9 大改造批次）
  - `docs/IMPLEMENTATION_PROGRESS.md` / `docs/LIGHTWEIGHT_AGENT_COMPLETION_AUDIT.md`（2026-07-16，历史快照）

---

## 1. 背景与现状基线

### 1.1 项目概况

CodeRook 是本地优先、BYOK（自带 Key）的双进程 AI 编码 Agent 运行时：`coderook-core` 常驻守护进程持有全部状态（会话、run、后台 worker、权限、持久化），`coderook-tui` / `coderook` 是薄客户端，经回环 TCP JSON-RPC 通信；另有 7438 端口 HTTP/SSE 持久运行时 API。

截至基准版本：42 个提交（约 3 周密集开发）、约 36,800 行源码、116 个测试文件约 23,000 行（841 用例）、ruff + mypy strict（双平台）+ wheel smoke 的完整质量链。代码内几乎无 TODO 残留——技术债全部显式记录在文档里。

### 1.2 优势保留项（本路线图不动摇的地基）

以下能力达到或超过市面同类产品设计水准，是差异化竞争力，后续阶段只在其上叠加，不推倒重来：

| 能力 | 现状 | 相对市面水平 |
|---|---|---|
| Durable runtime | 事件溯源、SQLite 投影、崩溃三层恢复、SSE 游标回放 | 领先（多数同类无崩溃恢复） |
| 并行工具执行 | ResourceClaim（路径/任务/后台任务）相交检测 + 同批只读去重 + 读缓存 LRU | 比 Claude Code 的"并行安全即并发"更精细 |
| 权限与 authority | 六层决策流 + 四维 authority（姿态 × 模式 × 动作 × 信任）+ 子代理单调收紧 | 设计完整度领先；**强制力落后**（沙箱 advisory-only，见阶段 3） |
| 子代理编排 | durable worker + worktree 隔离 + WriteClaim + 预算/恢复重试 + 结果五段契约 | 领先 |
| 工作流 | 声明式 TOML IR + SQLite 事件账本 + review_gate | 领先 |
| 上下文治理 | 结构化压缩 + 质量门禁 + 工具结果分级预算 + prefix fingerprint | 达标 |
| 协议契约 | 45 命令 / 45 事件类型化联合 + 生成式协议文档 CI 门禁 | 领先 |

### 1.3 核心判断

CodeRook 在"工程系统"维度（持久化、权限、编排、可观测、恢复）已是第一梯队；短板系统性地集中在**四个面**：

1. **感知与表达面**：无 Web 访问、无图片输入、LSP 仅 Python 事后诊断——Agent"看不见"代码库之外的世界。
2. **生态兼容面**：不读 `AGENTS.md` / `CLAUDE.md`（行业事实标准），从 Claude Code / Codex 迁移的用户第一分钟就撞墙。
3. **日常体验面**：TUI 缺一批"每分钟都会感受到"的基础设施（/help、输入历史、滚动控制、常驻状态栏、会话管理入口），且 4,176 行的 `tui/app.py` 上帝类已成结构债。
4. **经济与信任面**：无 $ 成本核算、无 thinking 预算控制、沙箱有名无实、无命令前缀级放行——重度用户的审批疲劳和成本焦虑无出口。

---

## 2. 市面对标分析

对标产品：Claude Code、OpenAI Codex、腾讯 CodeBuddy、Cursor、Gemini CLI、opencode、Aider。

### 2.1 产品能力矩阵

| 能力 | Claude Code | Codex CLI | CodeBuddy | Cursor | Gemini CLI | opencode | Aider | **CodeRook 现状** |
|---|---|---|---|---|---|---|---|---|
| 指令文件 | CLAUDE.md（分层+auto memory） | AGENTS.md（发起者，行业标准） | Rules | .cursor/rules | GEMINI.md | AGENTS.md | CONVENTIONS.md | ❌ 仅 .coderook/context.md |
| Web 访问 | WebSearch/WebFetch 原生 | 有（可联网浏览） | 有 | Agent 可联网 | Google 搜索 grounding | 依赖 MCP | ❌ | ❌ 无 |
| 图片输入 | 粘贴/拖入/读取 | 支持 | 支持 | 支持 | 支持 | 支持 | 支持 | ❌ 无 |
| 真实沙箱 | Seatbelt/bubblewrap 实际包裹 | Seatbelt/Landlock+seccomp 三档 | 云端隔离 | 云端隔离 | — | — | — | ❌ advisory-only |
| 命令前缀放行 | ✅ allowlist 前缀粒度 | ✅ | ✅ | — | ✅ | ✅ | ✅ | ❌ 仅 Tool.action 整体粒度 |
| Plan 模式 | ✅ | ✅ review mode | ✅ Craft 计划确认 | ✅ plan | ✅ | — | — | ✅ |
| Checkpoint/回滚 | ✅ rewind | ✅ | ✅ | checkpoint | — | — | git 自动 commit | ✅ |
| Skills/插件 | ✅ + 插件市场 | custom prompts | 技能库 | 扩展 | — | 插件 | — | ✅ skills（兼容 .claude/.codex 目录） |
| Hooks | ✅ 全生命周期 | 通知/CI | — | — | — | — | — | ✅ 11 事件 |
| MCP | ✅ 全 transport | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ 仅 stdio/TCP，无 HTTP/SSE，无管理 UI |
| 子代理 | ✅ subagents | ✅ cloud tasks 并行 | ✅ | Background Agents | — | — | — | ✅（durable 程度更高） |
| 成本显示 | ✅ /cost | ✅ | 用量面板 | ✅ | ✅ | ✅ | ✅ | ❌ 仅 token 计数 |
| thinking 控制 | ✅ | ✅ effort | — | — | — | — | — | ❌ 不主动请求 |
| LSP | ✅ 多语言 | ✅ | ✅ | ✅ 原生 | — | — | LSP 探索 | ⚠️ 仅 Python/pyright 事后诊断 |
| 持久 shell | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ 每次新进程 |
| Headless/SDK | ✅ stream-json | ✅ exec | — | — | ✅ | ✅ | ✅ 脚本化 | ⚠️ coderook run（无结构化输出格式） |
| IDE 集成 | ✅ VS Code | ✅ VS Code/Cursor/Windsurf | ✅ 全家桶（本源） | ✅ 原生 | ✅ | — | — | ❌ |
| 桌面/Web | Web + 代理产品 | Desktop + Web（云任务） | 桌面端 | Web | Web | Web | — | ❌（Tauri 方案文档已有） |
| 免登录使用 | 需订阅/API | ChatGPT 账号捆绑 | ✅ 免费额度大 | 订阅 | ✅ 免费额度 | ✅ BYOK | ✅ BYOK | ✅ BYOK |
| 多 provider | 需配置 | 主自家 | 自家 | 自家 | 自家 | ✅ | ✅ | ✅（3 wire format + route 体系） |

### 2.2 各产品设计要点速记

- **Claude Code**：交互细节打磨的标杆——常驻 context 指示器、/cost、输入历史（Ctrl+R 全文搜索）、自动会话标题、output styles、statusline 定制、subagent 汇总视图、"Opus 规划 Sonnet 执行"的模型分工。安全上 macOS/Linux 有实际 OS 沙箱。生态上 skills/hooks/plugins 市场形成飞轮。
- **Codex CLI**：安全模型的标杆——沙箱三档（read-only / workspace-write / danger-full-access）+ "沙箱内自动放行、失败再审批"策略，把审批疲劳降到接近零；AGENTS.md 成为跨厂商行业标准的发起者；已布局 CLI + IDE 扩展 + Desktop + Web 云任务四形态。
- **CodeBuddy**：国内生态标杆——Craft 智能体（计划-确认-执行多文件改动）、单元测试生成、代码审查、Rules 配置、腾讯云（CloudBase）部署闭环、大免费额度降低上手门槛；同时推出兼容 Claude Code 生态的 TUI（CodeBuddy Code）。
- **Cursor**：Tab 补全 + Agent + Background Agents（云端并行）+ memories；IDE 原生体验是 TUI 产品难以企及的参照，但它是云端产品，与 CodeRook 本地优先定位不同。
- **Gemini CLI**：免费额度 + Google 搜索 grounding 是获客利器；架构上无特别领先处。
- **opencode**：与 CodeRook 同构（client/server 分离、BYOK、TUI），是定位最接近的竞品；其主题系统、share links（会话分享网页）、社区生态运营值得借鉴。
- **Aider**：repo-map 与 git 深度自动集成（每个改动自动 commit）是老牌差异；benchmark 文化（可复现评测）值得学习。

### 2.3 CodeRook 的差异化定位

> **本地优先 + BYOK + durable 多 Agent 运行时**：不强制订阅、不依赖云、跨厂商路由、崩溃可恢复、全程可审计。

竞品要么捆绑自家模型订阅（Claude Code/Codex/Cursor/CodeBuddy），要么无 durable/审计能力（opencode/Aider）。CodeRook 应把"企业级可审计的本地 Agent 运行时"作为长期叙事，把"开发者每天用得顺手"作为当下主线——这正是本路线图的排序逻辑：**先让用户留下，再让优势可见**。

---

## 3. 差距清单与优先级矩阵

按"用户影响面 × 实现成本"分四象限。影响面按"目标用户每天都碰到"到"特定场景才碰到"递减。

### 3.1 高影响 × 低成本（立即做，阶段 0）

| # | 差距 | 现状证据 |
|---|---|---|
| G1 | 无 /help、无键位面板 | 斜杠命令只能靠补全弹窗发现 |
| G2 | 无输入历史（↑ 不能回溯） | ChatTextArea ↑ 是光标移动 |
| G3 | 强制贴底滚动，长输出无法回读 | 每次追加都 scroll_end |
| G4 | 无常驻 context 水位/成本显示 | 水位条只在 /context 输出里 |
| G5 | 会话 rename/fork/export/delete 无 TUI 入口 | IPC 与 CLI 均已支持 |
| G6 | 会话列表仅 10 条且无搜索 | SessionPicker 硬编码 |
| G7 | 无自动会话标题 | SessionPicker 显示 Untitled |
| G8 | /config 与 coderook configure 两套向导不一致 | TUI 仅 4 preset，CLI 支持任意 URL |
| G9 | 中英文案混杂 | 提示中文、命令描述英文 |
| G10 | Ctrl+C 复合语义易误触取消 run | 复制 vs 取消依赖鼠标选择 |
| G11 | 版本号 0.0.1 与成熟度不符；Dockerfile 0 字节 | pyproject + 根目录 |

### 3.2 高影响 × 中成本（阶段 1-2 主力）

| # | 差距 | 现状证据 |
|---|---|---|
| G12 | 无 AGENTS.md/CLAUDE.md 加载 | 仅 .coderook/context.md（skills 反而兼容 .claude/.codex 目录） |
| G13 | 无 WebSearch/WebFetch | TUI 已预留展示文案（tui/app.py:485）但工具不存在 |
| G14 | 无图片输入 | read_file 仅 UTF-8 文本 |
| G15 | LSP 仅 Python 事后诊断，事件未在 TUI 订阅 | core/lsp + lsp.diagnostics 事件闲置 |
| G16 | 无持久 shell 会话 | 每次 Bash.run 新进程，cd/env 不保持 |
| G17 | 无 $ 成本核算 | worker 只累计 token_usage；TurnReceipt cost 恒 unknown |
| G18 | 无 thinking/reasoning 预算 | Anthropic 从不请求 thinking；Responses 不传 effort |
| G19 | max_steps=20 无续跑机制 | 到顶即 exceeded_max_steps 失败 |
| G20 | OpenAI 系不感知缓存节省 | cached_tokens 读到但不展示 |
| G21 | 模型路由仅 static（配置项已预留 rule_based/cost_budget） | llm/ 路由层 |

### 3.3 高影响 × 高成本（阶段 3-4 攻坚）

| # | 差距 | 现状证据 |
|---|---|---|
| G22 | 沙箱 advisory-only，无实际隔离 | detect_sandbox_capability 只探测不包裹 |
| G23 | 无命令前缀级 always-allow | policy key 是 Tool.action 整体 |
| G24 | 无逐块 diff 审阅 | 权限卡片只展示参数不展示 diff |
| G25 | tui/app.py 4,176 行上帝类 | 已有 TUI_REFACTOR_PLAN.md 未执行 |
| G26 | /mcp /hooks /memory /jobs 无管理 UI | daemon 能力完整存在，纯 UI 缺失 |

### 3.4 低影响或高成本（阶段 5 占位）

| # | 差距 | 说明 |
|---|---|---|
| G27 | IDE 扩展 | HTTP API+SSE 已就绪，技术基础最好 |
| G28 | headless 结构化输出（stream-json） | coderook run 已有，缺 --output-format |
| G29 | MCP Streamable HTTP/OAuth/resources/prompts | 当前 stdio/TCP 够用 |
| G30 | Desktop（Tauri）/插件市场/跨设备 | PC_DESKTOP_MIGRATION_PLAN.md 已有方案 |
| G31 | notebook 编辑、Vim 模式、statusline 定制 | Claude Code parity 的长尾 |

---

## 4. 分阶段计划

每阶段含：目标、工作项（含代码位置）、验收标准、对标依据。工作项编号 `W<阶段>.<序号>`。

### 阶段 0（第 1-2 周）：体验快赢 ✅ 已完成（2026-08-16，commit 21bfb24）

> 状态：W0.1-W0.6、W0.8 全部完成；W0.7 部分完成（permission 超时提示、busy 文案、llm.retry 中文化、Agent 忙碌提示已做；**/config 与 coderook configure 向导统一顺延**——放在阶段 4 命令注册表重构后一并处理，避免继续加深 app.py 斜杠分支）。

> 目标：让现有用户"每天都能感受到改善"，新用户 5 分钟内跑通第一个任务。全部是低风险、无协议变更的 TUI/CLI 层工作。

**W0.1 /help 命令与键位面板**（G1）
- 新增 `/help`：渲染键位表（Tab/Shift+Tab/Ctrl+C/Ctrl+Q/Ctrl+Shift+C）+ 全部内建斜杠命令（名称/参数/一句话说明）。
- 补全弹窗项带 usage 行（如 `/permissions <ask|auto-review|full-access>`）。
- 位置：`src/code_rook/tui/app.py`（斜杠分发 `on_chat_text_area_submitted`、`_build_slash_items`）。
- 验收：/help 一屏可读；新用户不读 README 能发现全部命令。

**W0.2 输入历史**（G2）
- ↑/↓ 在输入框空或光标在首/末行时回溯已提交输入；持久化到 `~/.coderook/tui-history.jsonl`（上限 500 条，去重连续重复）。
- 对标：Claude Code / Codex / CodeBuddy TUI 均有。

**W0.3 智能滚动**（G3）
- 用户上滚（scroll_y 偏离底部超过阈值）后停止自动贴底；显示"↓ N 行新内容 · 点击回到底部"指示条；点击或按 End 恢复跟随。
- 位置：`tui/app.py` 事件追加路径的 scroll_end 调用。

**W0.4 常驻状态栏**（G4，为阶段 2 成本显示预留位置）
- 顶栏右侧追加：context 水位缩略条（复用 /context 的 █░ 渲染，70%/85% 变色）+ 当前会话 token 数。
- 数据源：已有 `_last_context_pct`（llm.usage 事件维护）。

**W0.5 Ctrl+C 语义拆分**（G10）
- 有鼠标选择 → 复制；无选择且 run 进行中 → 取消 run；无选择且空闲 → 显示"Ctrl+Q 退出"提示（不退出，避免误触）。
- 取消 run 前若 run 已产出 >1 步，加一次确认（"再次 Ctrl+C 确认取消"）。

**W0.6 会话管理 TUI 化**（G5/G6/G7）
- `/rename [title]`、`/fork`、`/export [md|json]`、`/delete`（delete 需确认）；全部走已存在的 session.rename/fork/export/delete IPC。
- SessionPicker：改为可搜索（输入即过滤标题/ID）、limit 提升到 50、增加动作键（r 重命名 / d 删除）。
- 自动会话标题：首个 run 完成后后台用当前 route 生成 ≤20 字标题（一次性小请求，可配 `[tui] auto_title = false` 关闭）；标题写回 session store。
- 对标：Claude Code 自动会话命名 + /rename。

**W0.7 向导与文案一致性**（G8/G9）
- `/config` 增加"自定义 OpenAI 兼容端点"选项（复用 CLI `provider add --wire-format --base-url` 的路径），消除两套向导差异。
- 文案基准定为中文（现有 UI 主语言）；命令描述、事件文案（"Approval required"、"retrying model response" 等）统一中文化，专有名词保留英文。
- `permission.denied`（超时/断连）显示明确提示行而非静默移除卡片。
- busy 时 /model、/provider、/doctor 改为排队提示（"将在当前 run 结束后可执行"），不再硬拒绝。

**W0.8 版本与交付卫生**（G11）
- 版本号 0.0.1 → 0.1.0（语义：核心链路完整、产品外围迭代中）。
- 补 Dockerfile（python slim + uv + coderook-core，暴露 7437/7438，容器内跑 headless）并加入 wheel smoke。

**阶段 0 验收**：新用户从安装到首个任务完成 ≤5 分钟；会话生命周期操作（建/切/改名/导出/删）全程不离开 TUI；长输出回读不被打断。

---

### 阶段 1（第 2-5 周）：生态兼容 + 感知能力 ✅ 已完成（2026-08-16，commits 034afd2 / 2c98682 / W1.5 批次）

> 状态：五个工作项全部落地，其中两项按"同等价值、更低风险"的路径实现，偏差记录如下，后续批次可按需升级：
> - **W1.3 图片输入**：实现为独立 `read_image` 工具（非扩展 read_file）；图片仅随下一次模型请求发送、随后替换为文本占位符（防止 base64 永久占据历史与计费）。**TUI 粘贴图片未做**，保留为后续项（依赖终端剪贴板图片读取，优先级低）。
> - **W1.4 LSP 跨语言**：实现为一次性 CLI 命令分派（.py→pyright、.ts/.tsx→tsc）而非常驻 LSP server；TUI 已订阅 `lsp.diagnostics` 实时渲染。**Go/Rust 未接**，语言表可扩展；常驻增量 LSP 保留为远期项。
> - **W1.5 持久 shell**：实现为"常驻进程 + 管道 + sentinel 完成探测"（无 pywinpty 新依赖，Windows/POSIX 双平台）；`Bash.run` 新增 `session: isolated|persistent` 参数，**默认 isolated**（保守起见，模型可显式选择 persistent 共享 cwd/env/venv）；daemon 级池按 session_id 复用、空闲 30 分钟回收。**与 BackgroundJobRegistry 的整合未做**（后台任务仍走独立进程）。

> 目标：跨过迁移门槛（AGENTS.md），让 Agent 看见代码库之外的世界（web/图片）与更多语言（LSP）。这一阶段决定 CodeRook 能否承接 Claude Code / Codex 的外溢用户。

**W1.1 AGENTS.md / CLAUDE.md 指令文件加载**（G12）⭐ 迁移门槛第一优先
- 加载顺序（存在即生效，全部注入动态层的项目段）：项目 `AGENTS.md` → 项目 `CLAUDE.md` → 项目 `.coderook/context.md`（coderook 专属后置覆盖）；全局同理（`~/.coderook/context.md` 唯一，不读全局 AGENTS.md 避免歧义）。
- 位置：`core/context.py` 项目上下文组装处；注意保持 prefix fingerprint 稳定——指令文件归入"项目动态段"，变更时 fingerprint 事件应正确反映（现有 Global/Project context 段已在该层）。
- 会话启动时在 TUI 显示"已加载指令文件：AGENTS.md, context.md"，让迁移用户确认生效。
- 对标：AGENTS.md 已是 Codex/Gemini CLI/opencode 等共同支持的行业标准；Claude Code 用 CLAUDE.md。

**W1.2 WebFetch / WebSearch 工具**（G13）⭐ 感知面第一优先
- `WebFetch(url, prompt)`：无 key 依赖，HTML→Markdown（引入轻依赖或手写转换），重定向白名单、超时、单页体积上限（复用 64KB 截断策略），SSRF 防护（拒绝内网网段/localhost/file 协议）。
- `WebSearch(query)`：可配三档——provider 原生（Anthropic web_search tool / OpenAI 内置）、SearXNG/DuckDuckGo（免 key）、Brave/SearchAPI（key 经 CredentialStore 引用）；配置段 `[agent.web_search]`。
- 权限：external 动作族，默认 ASK；域名 allowlist 持久化（policy.toml）；`always_allow` 支持域名粒度（`WebFetch:docs.python.org`）。
- TUI 展示文案映射已存在（`tui/app.py:485`），实现落地即自动点亮。
- 对标：Claude Code WebSearch/WebFetch；Gemini CLI 搜索 grounding。

**W1.3 图片输入（多模态）**（G14）
- `read_file` 扩展：检测 png/jpg/webp/gif → base64 + media_type，作为 image content block 进入对话（Anthropic Messages 与 OpenAI chat/responses 均支持）；route 不支持多模态时给出明确错误文案。
- TUI 粘贴图片：Windows 经 PowerShell `Get-Clipboard -Format Image`、macOS `osascript`、Linux `xclip`（复用 `tui/clipboard.py` 的平台分派模式）；图片落 artifacts（content hash 寻址），消息显示 `[图片 sha256:abcd1234]`。
- 对标：全部主流产品均支持。

**W1.4 LSP 跨语言 + 诊断实时化**（G15）
- 常驻 LSP 客户端：TypeScript（vscode-langservers-extracted）、Go（gopls）、Rust（rust-analyzer），按 workspace 文件类型懒启动；沿用 pyright 的失败降级策略。
- 诊断实时推送：`lsp.diagnostics` 事件改为增量推送（文件保存/编辑后），TUI 订阅该 topic（当前未订阅）并在工具行下方渲染红/黄诊断行；`/context` 与 /turn 面板透出。
- 语言集做成 `[lsp]` 配置（命令名 + 启动参数），新语言零代码接入。
- 对标：Claude Code / Codex / CodeBuddy 的多语言诊断；CodeRook 的"编辑后自动诊断注入"机制保留为兜底。

**W1.5 持久 shell 会话**（G16）
- daemon 级常驻 shell：POSIX 用 pty、Windows 用 ConPTY（pywinpty）或降级 PowerShell 常驻进程；会话键 = session_id，idle 超时回收（30 分钟）。
- `Bash.run` 增加参数 `session: persistent|isolated`（默认 persistent，权限卡显示"在持久会话中执行"）；`cd`/env/venv 激活跨调用保持。
- 与 BackgroundJobRegistry 整合：持久会话本身注册为可 inspect 的后台对象。
- 对标：Claude Code / Codex / CodeBuddy TUI 的 shell 状态保持。

**阶段 1 验收**（✅ 已达成，唯持久 shell 默认 isolated 需模型显式选择 persistent）：仓库放一个 AGENTS.md 立即生效；能搜索"最新 pydantic v2 变更"并引用网页内容；贴一张报错截图让 Agent 分析（read_image 工具读取，TUI 粘贴待做）；TypeScript 项目编辑后有实时红肿诊断；连续 Bash 调用 `cd` 状态保持（session=persistent）。

---

### 阶段 2（第 5-8 周）：智能与经济性 🔶 进行中（2026-08-17）

> 状态：**W2.1 成本核算 ✅**（pricing 模块：内置参考价 + `~/.coderook/pricing.toml` 用户覆盖 + 前缀匹配；`llm.usage` 事件新增 `model` 字段；TUI 顶栏常驻累计成本、`/cost` 分解视图含缓存节省与无价模型提示；TurnReceipt 侧成本落地为顺延项）。**W2.2 thinking 预算 ✅**（route 新增 `thinking: off|low|medium|high`；Anthropic 映射 budget_tokens 并同步抬高 max_tokens、Responses 映射 `reasoning.effort`、openai_chat 映射 `reasoning_effort` 且保留 DeepSeek 域名默认高推理兼容；PLAN 模式在 route 启用 thinking 时自动升 high 档；TUI `/provider` 显示档位）。**W2.3 缓存深化 ✅ 部分完成**（Anthropic 增量缓存断点 + OpenAI cached_tokens 节省展示）。**W2.4 模型路由实装 ✅**（`llm/router.py`：`rule_based` 按 PLAN/ACT 模式选路由 + `cost_budget` 按单 run 累计成本降档；`loop.py` 每步经 `route_refresher` 回调重取 active route 实现 **/model 切换下一 turn 即生效**、事件收据随之更新）。**W2.5 步数续跑 ✅**（交互 ask 续跑 ≤3 段 + `[agent] max_step_continues` 自动续段）。**W2.5b 子代理预算硬顶 ✅**（预算到顶强制收尾：上下文置 budget_limited、Worker 终态 BUDGET_LIMITED、结果为空时合成含 `budget_exhausted` 标记的收尾 SUMMARY 交回父上下文）。阶段 2 全部收尾，见 §5.6 阶段 2 复盘。

> 目标：让重度用户"用得起、看得清、跑得完"——成本可见、推理可控、缓存可省、长任务不断。

**W2.1 $ 成本核算与 /cost**（G17）
- `llm/model_catalog` 增加单价字段（input/output/cache_read 每 M token，USD）；来源优先级：`~/.coderook/models.toml` 用户覆盖 > 内置表；未知模型显示 token 并标注"单价未知"。
- 常驻显示：顶栏 session 累计成本（W0.4 状态栏预留位）。
- `/cost` 命令：本会话 / 今日 / 按模型 / 按工具（LLM 调用 vs 子代理）分解；缓存节省单独一行（见 W2.3）。
- TurnReceipt 的 cost 字段落地真实值（runtime store 已有字段）。
- 对标：Claude Code /cost、Codex 用量面板、Cursor 成本显示。

**W2.2 thinking / reasoning 预算**（G18）
- route 配置增加 `thinking = off|low|medium|high`：Anthropic 映射 budget_tokens 档位（thinking block 回传签名链路已就绪，只差主动请求）；OpenAI Responses 映射 `reasoning.effort`；openai_chat 映射 DeepSeek 风格开关（去除当前域名硬编码）。
- 计划模式联动：PLAN 模式自动升到高推理档、ACT 模式回落（对标 Claude Code "Opus plans, Sonnet executes" 的分工思想，CodeRook 用同一模型的 thinking 档位实现）。
- TUI /model 显示当前 thinking 档位；/turn 面板展示本 turn thinking token 消耗。

**W2.3 prompt caching 深化**（G20）
- Anthropic：在工具结果累积的消息中段追加 cache breakpoint（系统提示+工具目录已有 ephemeral），长会话命中率提升。
- OpenAI：cached_tokens 已读出——累计展示"缓存命中节省 $X"（/cost 与 /context）。
- prefix fingerprint 事件已有：TUI 顶栏在 fingerprint 变化时提示"前缀已变更，缓存重置"。

**W2.4 模型路由实装 + per-turn 切换**（G21）
- 实装配置项 `router = rule_based | cost_budget`（当前仅 static）：rule_based 支持"PLAN 用 route A（高推理）/ACT 用 route B（快）"与"纯问答用轻量模型"两条内置规则；cost_budget 按预算自动降档。
- `/model <id>` 切换从"下一 run 生效"改为"下一 turn 生效"（loop 每 turn 从 route registry 重取 active route，事件收据随之更新）。
- 对标：Claude Code /model 即时切换；Codex model router。

**W2.5 max_steps 续跑与子代理预算硬顶**（G19）
- 交互模式：达到 max_steps 时发 ask_user 结构化提问"已到步数上限，继续/停止"，继续则追加 max_steps 续段（上限防失控：连续续段 ≤3 次）。
- headless：`coderook run --max-steps-continue N` 配置。
- 子代理 token_budget 从"仅记录"改为硬顶：到顶触发强制收尾（合成结果 + SUMMARY 标注 budget_exhausted）。

**阶段 2 验收**：每个会话能看到花了多少钱、缓存省了多少；PLAN 模式自动高推理；长任务不再因 20 步静默失败；/model 切换下一 turn 即生效。

---

### 阶段 3（第 8-11 周）：安全与可信 ⬜ 未开始

> 目标：把"权限模型设计领先但强制力落后"的错位补齐——真沙箱 + 前缀放行，让 AUTO_REVIEW 姿态下的审批疲劳归零。对标 Codex 的沙箱三档是本阶段的锚。

**W3.1 真实 OS 级沙箱执行**（G22）⭐ 本阶段核心
- Linux：bwrap 包裹（read-only：只读挂载工作区；workspace-write：可写工作区 + 临时目录；禁网络可选）；bwrap 缺失时降级当前行为并在 /sandbox 标注。
- macOS：sandbox-exec profile（同两档语义）。
- Windows：Job Object + 受限令牌 + 路径 ACL（最简实现：阻断工作区外写入）；实现难度最高，允许降级为"路径写入审计 + 提示"，但必须诚实标注（延续 advisory 的诚实化传统）。
- **权限闭环（关键设计）**：AUTO_REVIEW 姿态下，沙箱 read-only/workspace-write 内的命令自动放行；沙箱失败/越界写 → 回落 ASK。这把六层权限流的 Tier 6 与沙箱档位打通，是对标 Codex "sandbox + on-failure approval" 的核心。
- 位置：`core/permissions/`（决策流接入沙箱判定）、新 `core/sandbox/`（执行包裹）、`core/tools/builtin/bash.py`（执行路径）。
- 验收：AUTO_REVIEW 下 `rm -rf /tmp/x`（工作区外）触发审批，工作区内 `uv run pytest` 零审批；跨平台 CI 各有一档可用。

**W3.2 命令前缀级 always-allow**（G23）
- policy key 从 `Tool.action` 扩展支持前缀模式：`Bash.run:uv run pytest*`、`Bash.run:git status`（fnmatch 语义）；审批卡新增"始终允许此命令模式"选项（第四选项 d→新增选项，重新排布 y/a/n/d/m）。
- outside-cwd 启发式修复：`cd <workspace 内子目录>` 不再触发强制 ASK（当前误报）。
- 验收：`git status` 一生只批一次；前缀匹配有单元测试覆盖注入场景（`uv run pytest; rm -rf /` 不得命中 `uv run pytest*` 的前缀——用首 token 解析而非纯 fnmatch）。

**W3.3 结构化 diff 审阅**（G24）
- edit/apply_patch 的权限卡内嵌 diff 视图（复用 /diff 的渲染），第一版：查看完整 diff + 接受全部/拒绝全部；第二版：逐 hunk 接受/拒绝（拒绝的 hunk 转为反馈注入下一轮）。
- 对标：Claude Code diff 审批、CodeBuddy Craft 的改动确认、Codex review mode。

**W3.4 §20 遗留清理**
- 全局订阅 scope 多客户端事件互见（§20 #18）：scope 校验收紧到连接级。
- artifact/blob GC（§20 #17）：按年龄+引用计数的保留策略，`/config gc` 手动触发 + 30 天默认清理，删除前展示清单。
- 家族工具内部分派绕过调用管线（§20 #10）：本阶段只做设计评审，实装放阶段 4 后（涉及 family 适配层重构，与 TUI 重构解耦）。

**阶段 3 验收**：一次 30 分钟真实任务中审批次数 ≤3（当前常态为 10+）；恶意/误操作命令在沙箱档位下无法写出工作区；diff 审阅可逐块拒绝。

---

### 阶段 4（第 11-14 周）：TUI 重构 + 管理面板 ⬜ 未开始（已并入一项顺延工作：W0.7 的 /config 向导统一）

> 目标：先拆上帝类（执行项目自己的 TUI_REFACTOR_PLAN.md），再在干净骨架上补管理面板——顺序不可颠倒，否则 app.py 会继续膨胀。

**W4.1 执行 TUI 五阶段拆分**（G25）
- 按 `docs/TUI_REFACTOR_PLAN.md`：控件外迁（widgets/）→ 连接层（client/）→ 命令注册表（commands/）→ IPC 封装 → 事件渲染器（render/）；不改交互语义。
- 拆分后单文件 <500 行；`/` 命令注册表化使新命令 = 一个声明 + 一个 handler（为 W4.2/W4.3 降成本）。
- 每拆一阶段跑全量 TUI 单测（65 个）+ 手工冒烟清单。

**W4.2 管理面板四件套**（G26）
- `/mcp`：server 列表（名称/transport/状态/工具数）、工具清单展开、启停（写回 config.toml 需确认）、失败原因透出。
- `/hooks`：hook 配置表 + 最近 100 条执行记录（hook.executed 事件，补 TUI 订阅）+ 手动重跑。
- `/memory`：当前项目记忆条目（memory 工具的 search/list 能力暴露）、删除、来源 session 跳转。
- `/jobs`：后台任务中心——运行中任务列表（background.* 事件已有）、attach 查看增量 stdout、取消；并行子代理结果统一汇总视图（对标 Claude Code subagent 汇总）。
- 全部为已存在 daemon 能力的 UI 化，唯一新增 IPC 可能是 mcp.list/hooks.list 查询命令（走 bus 命令 → 同步 WIRE_PROTOCOL.md）。

**W4.3 补全与输入体验深化**
- 补全弹窗：名称 + 描述模糊匹配；选中项显示 usage 与参数提示。
- 斜杠参数 Tab 补全（如 /permissions 的枚举值、/model 的模型 ID）。

**阶段 4 验收**：app.py 家族全部 <500 行；MCP/hooks/memory/后台任务管理全程 TUI 内完成；新增一条斜杠命令 ≤30 行代码。

---

### 阶段 5（第 14 周+）：形态扩展（方向占位，不做展开）

按技术基础成熟度排序：

1. **IDE 集成（VS Code 扩展）**——技术基础最好：HTTP API（7438）+ SSE 游标回放已就绪，扩展只需消费 thread/turn/items 接口；收益是打开 CodeRook 到 IDE 用户群。
2. **headless SDK**：`coderook run --output-format json|stream-json`（对标 `claude -p --output-format stream-json`、Codex exec），解锁 CI/脚本编排场景——durable runtime 本就是这个场景的卖点。
3. **MCP 协议补全**：Streamable HTTP transport、OAuth、resources/prompts 入口、断线重连——跟随 MCP 生态演进。
4. **Desktop（Tauri）**：`docs/PC_DESKTOP_MIGRATION_PLAN.md` 已有方案；触发条件 = TUI 重构完成且 IDE 扩展验证了 HTTP API 稳定性。
5. **插件市场 / 跨设备同步 / 云协同**：远期，与"本地优先"定位的张力需单独决策（可参考 opencode 的 share links 作为轻量折中）。

---

## 5. 横切事项

### 5.1 工程纪律（每阶段收尾必跑完整 CI gate）

```
uv run ruff check .
uv run python scripts/check_brand.py
uv run mypy src
uv run mypy --platform linux src
uv run pytest -q
uv run python scripts/gen_protocol_doc.py --check
uv build && uv run python scripts/smoke_wheel.py dist
```

- 涉及 bus 模型的工作项（W1.2 权限域、W2.4 路由事件、W4.2 查询命令等）须同步 `WIRE_PROTOCOL.md` 并随同提交。
- 新增依赖（pywinpty、HTML 转换、LSP server 进程）须评估 wheel 体积与 Windows/Linux 双平台可用性。
- 所有新函数遵循单行中文注释规范；测试函数两行注释（功能/设计）规范。

### 5.2 成功指标

| 指标 | 现状 | 目标（阶段 3 末） | 度量方式 |
|---|---|---|---|
| 单次典型任务审批次数 | ~10+ | ≤3 | TurnReceipt 审批统计聚合 |
| 新用户首次任务时间 | 未测 | ≤5 分钟 | 手工脚本计时（阶段 0 验收项） |
| prompt 缓存命中率 | 无统计 | Anthropic route ≥60% | usage cache_read/input 比 |
| 长任务（>20 步）完成率 | 到顶即失败 | 支持续跑 | run 终态统计 |
| 任务成功率基准 | 无 | 建立轻量 benchmark（20 个跨语言真实任务，回归跑） | scripts/benchmark |

### 5.3 风险与依赖

| 风险 | 影响 | 缓解 |
|---|---|---|
| Windows 沙箱实现复杂度 | W3.1 交付延期 | 降级路径已内置（路径审计 + 诚实标注）；Linux/macOS 先行 |
| WebSearch 依赖外部服务 | W1.2 体验不一致 | 免 key 的 DDG 兜底 + provider 原生优先；失败文案明确 |
| 图片输入依赖模型多模态 | route 不支持时报错 | 能力探测 + 明确错误提示 + route 标注 |
| TUI 重构引入回归 | 阶段 4 风险 | 65 个 TUI 单测先行 + 冒烟清单 + 交互语义冻结 |
| 单价表维护成本 | W2.1 过期 | models.toml 用户覆盖 + 社区价格表定期同步 |
| 与本地优先定位的张力 | 阶段 5 云协同 | 每项形态扩展单独决策，默认本地 |

### 5.4 阶段复盘记录

**阶段 0 复盘（2026-08-16，commit 21bfb24）**
- 全部 8 个工作项按计划完成（W0.7 部分完成，/config 向导统一顺延至阶段 4）；实际一个批次内完成，未占用原估 2 周的全部时间。
- 计划外收获：修复两个存量缺陷（ping 集成测试硬编码版本断言、hooks 测试在中文 Windows 的 tasklist GBK 解码崩溃）——CI 在本机恢复全绿。
- 经验：TUI 测试用 `run_test` 挂载真实 App 的 harness 模式可靠；智能滚动测试需显式 `scroll_end` 建立基线，不能依赖挂载时序。

**阶段 1 复盘（2026-08-16，commits 034afd2 / 2c98682 / W1.5 批次）**
- 五个工作项全部落地，三项按"同等价值、更低风险"路径实现（图片经独立工具而非 read_file 扩展、诊断经一次性 CLI 而非常驻 LSP、持久 shell 经管道+sentinel 而非 ConPTY——最后者避免了 pywinpty 新依赖）。
- 计划外收获：图片"只随下一次请求发送、之后占位"的机制防止 base64 永久占据历史与计费；web 工具重定向逐请求校验补齐了 SSRF 的重定向绕过面。
- 顺延项（已并入后续阶段或转为独立小项）：TUI 粘贴图片（低优先级）、Go/Rust 诊断接入、持久 shell 与 BackgroundJobRegistry 整合、常驻增量 LSP。
- 下一步：阶段 2 从 W2.1 成本核算起步（model_catalog 单价表 + /cost + 常驻显示），随后 W2.2 thinking 预算。

### 5.5 暂停点记录（2026-08-17 已清账 ✅）

W2.5 步数续跑与 W2.3 增量缓存断点的 WIP 缺陷已修复并提交：

- **续段配额翻倍 bug**：`loop.py::_try_continue_past_max_steps` 首次触达时固定 `_initial_max_steps`，续段按固定初始配额追加（2→4→6→8），不再随上限翻倍。
- **`provider.py::with_incremental_cache_breakpoint` 类型错误**：`content` 经 `isinstance(raw, list)` 收窄后重建块列表，移除全部 ignore 注释。

**W2.5 ✅ 已完成**：步数耗尽时交互模式经结构化提问续跑（上限 3 段，选项"继续执行/就此停止"），`[agent] max_step_continues`（env `CODEROOK_MAX_STEP_CONTINUES`）配置自动续段数供 headless 使用。
**W2.3 ✅ 部分完成**：Anthropic 增量缓存断点（最后一个 tool_result 块打 ephemeral 标记，受 route `supports_prompt_cache` 开关控制）；OpenAI cached_tokens 节省展示已由 W2.1 /cost 覆盖。

**随后待办**：W2.5b 子代理 token_budget 硬顶（顺延项）；阶段 3 沙箱起步。

### 5.6 阶段 2 复盘（2026-08-17，commit 阶段2批次）

**W2.4 模型路由实装 + per-turn 切换 ✅**：新增 `llm/router.py`（`RoutingPolicy` + `select_route_id` + 纯问答降档启发式）；新增配置项 `llm.router_plan_route / router_act_route / router_cost_budget / router_cost_fallback`；`AgentLoop` 新增可选 `route_refresher` 回调，每步 model 请求前重取 active route——`/model` 切换下一 turn 即生效；`cost_budget` 经订阅 `llm.usage` 累积估算成本、超阈值降档到 fallback 路由，并重新发布 `LlmRouteSelectedEvent` 收据。静态/规则/预算三策略单测覆盖。
**W2.5b 子代理 token_budget 硬顶 ✅**：预算到顶由根目标预算账本（`registry.py`）判定位移并置活跃上下文 `budget_limited`；终态回写 `WorkerStatus.BUDGET_LIMITED`；当被中断导致结果为空时，`tool.py` 用 `synthesize_budget_summary` 合成含 `budget_exhausted` 标记的收尾回执，父上下文拿到结论性而非空结果。现有 `test_agent_budget_exhaustion_stops_worker` 扩展断言该标记。
**收益**：成本可见（$）、推理档位可控、缓存可省、长任务续跑不静默失败、`/model` 即时切换、子代理预算硬顶不再产生空结果。
**接下来**：阶段 3 安全与可信——W3.1 真实 OS 沙箱 + 权限闭环为首要。

---

## 6. 附录：文档体系与基准关系

| 文档 | 状态 | 说明 |
|---|---|---|
| docs/FUNCTIONAL_ARCHITECTURE.md v1.1 | ✅ 现行权威 | 架构深描 + §20 已知问题清单（本路线图的输入） |
| docs/TUI_REFACTOR_PLAN.md | ✅ 现行 | 阶段 4 W4.1 的执行依据 |
| docs/ADR_RUNTIME_CONTRACT.md | ✅ 现行 | 持久运行时契约决策 |
| docs/OPTIMIZATION_ROADMAP.md（本文档） | ✅ 现行 | 产品优化路线图（取代下列历史差距文档） |
| docs/CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS.md | 🗄 历史 | 2026-07-15 快照，缺口多数已修复 |
| docs/CLAUDE_CODE_EXPERIENCE_PARITY.md | 🗄 历史 | 2026-07-30 评分 81/100，早于 R1-R9 批次 |
| docs/IMPLEMENTATION_PROGRESS.md | 🗄 历史 | 2026-07-16 快照 |
| docs/PC_DESKTOP_MIGRATION_PLAN.md | ✅ 远期参考 | 阶段 5 Desktop 方案 |

> 维护约定：本路线图按阶段推进时更新各工作项状态（未开始/进行中/已完成/已裁剪），每阶段收尾追加"阶段复盘"小节（实际工时 vs 估计、指标变化）。下一次全量差距重估建议在阶段 3 结束时进行。
