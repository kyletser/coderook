# CodeRook 简历证据账本

本文件控制简历、README 和面试中的数字口径。规则很简单：稳定结构事实可以直接写；精确运行数字
必须绑定日期、commit 和报告；外部门禁没有报告就不能把 workflow 配置写成实际成绩。

## 推荐项目标题

**CodeRook｜本地优先 Durable Multi-Agent Coding Runtime｜个人项目**

适合投递 Agent Infrastructure、AI Agent Engineer、Python Backend/Platform。若岗位更偏应用，可把
第一条提前到工具闭环；若偏后端，优先讲协议、持久化、恢复与可观测。

## 当前可直接使用的事实

| 简历事实 | 公开证据 | 使用边界 |
|---|---|---|
| daemon/client 双进程，本地 loopback JSON-RPC/NDJSON + HTTP/SSE | `docs/FUNCTIONAL_ARCHITECTURE.md`、`WIRE_PROTOCOL.md` | 可写“设计并实现”，不要写分布式集群 |
| File/Git/Bash/Run 四类工具与六层权限决策 | architecture、threat model、permission tests | 四类是 action family，不等于只有四个工具 |
| task/subagent/fleet/worktree/workflow 编排 | workflow IR/ledger 与相关 tests | 可写并发约束，不能写效果提升百分比 |
| 50 个固定离线任务，baseline 50/50 按预期失败 | `docs/RELEASE_SCORECARD.md`、benchmark fixtures | 只证明 verifier 合同，不是模型 0% 或候选成绩 |
| 1,000+ 自动测试门禁 | release scorecard 的 dated checkpoint | 使用范围值，避免每次提交后精确数过期 |
| Windows/Ubuntu/macOS CI 与发行 workflow | commit `fe3bd3b` 的连续三次 CI、Distribution `32365931473` | 可写“三平台 CI 连续三次及干净发行门禁全绿”，不能扩写为生产就绪 |
| SBOM、SHA256SUMS、OIDC provenance、Cosign keyless workflow | `docs/RELEASING.md` | 可写“设计链路”，首次真实 Release 前不能写“已发布可验证产物” |

## 可引用的历史精确数字

这些数字必须连同限定词使用：

| 数字 | 日期/基线 | 推荐写法 |
|---|---|---|
| `1163 passed, 2 skipped` | 2026-08-20，commit `fe3bd3b` 本地完整 gate | “在 2026-08-20 本地完整门禁中 1,163 项通过、2 项平台跳过” |
| `8.20 s` | 同一 commit 的 installed-wheel first-run smoke | “该 Windows 本机样本为 8.20 秒”，不写成跨平台 P95 |
| 三平台各 `100/100` | 2026-08-20，Crash Recovery `32365937235`，commit `3e4a1c4` | 可写“Ubuntu/macOS/Windows 合计 300 次 daemon 强杀恢复全部通过”；不外推整机断电 |
| 三平台安全负例 100% | 2026-08-20，CI `32365530943`，commit `3e4a1c4` | 必须同时说明 Windows 是 degraded + ASK，不是 OS sandbox |
| 连续三次三平台完整 CI | 2026-08-20，`32368432365`、`32368834608`、`32369245347`，commit `fe3bd3b` | 可写 CI 稳定性门禁达标；后续回退必须同步撤回 |
| 三平台 clean wheel + Docker + Windows portable + VSIX/Extension Host | 2026-08-20，Distribution `32365931473`，commit `3e4a1c4` | 可写候选安装门禁通过；不能写已发布 PyPI/GHCR/Marketplace |
| `4,176 -> ~2,110` 行 | 2026-08-17 TUI 拆分阶段；管理功能加入后约 2,246 行 | 用于解释重构范围，不写“减少 50% 复杂度” |
| `+95` 个 TUI 相关测试 | 同一 TUI 阶段复盘 | 只描述新增回归保护，不推导缺陷率下降 |

精确数字的权威入口是 `docs/RELEASE_SCORECARD.md` 和带 commit 的复盘。运行新完整 gate 后，应新增
日期记录或更新数字，不静默把旧 checkpoint 当成 HEAD 结果。

## 等真实报告后才能替换的句子

以下模板只允许用真实报告填写，不保留 `X/Y/Z` 占位后发布：

> 在固定模型、wire format、route 与预算下完成 N 个自建任务和 M 个公开任务，总体 pass@1 为 X%，
> 多文件为 Y%，单任务成本 P50/P95 为 A/B；三平台各 100 次 daemon 强杀恢复率为 Z%，报告包含失败
> 聚类、commit、容器 digest 与完整配置。

数据源必须是 benchmark artifact、官方 harness 输出、crash matrix 报告和不可变 commit。只挑最好一次、
更换失败任务、混用不同模型或不报告成本都不合格。

## 当前禁止宣称

- “生产级 / production-ready / 企业级安全”；
- “SWE-bench 达到 X%”或“Aider Polyglot 达到 X%”；
- “已发布 PyPI/GHCR/VSIX”或“供应链 attestation 已验证”；
- “多 Agent 提升 X%”“降低成本 X%”“用户增长 X%”；
- Windows 有 OS 文件系统/网络沙箱，或支持按域名强制联网白名单。

这些结论目前在 `docs/RELEASE_SCORECARD.md` 中明确为未运行或外部门禁；已经取得的 CI、安全与恢复数字必须带 commit/run 限定。

## 本人与第三方边界

**本人实现**：架构与协议、Agent loop 集成、工具/权限管线、PatchPlan、持久化与恢复、多 Agent 控制面、
TUI/SDK/VS Code 原型、评测适配、测试与发行合同。

**第三方能力**：模型推理与模型训练、Pydantic/Textual/SQLite 等库、bwrap/Seatbelt OS 机制、MCP 规范、
GitHub Actions、Syft、Cosign/Sigstore、Aider/SWE-bench 数据与官方 harness。简历可以写“接入/适配/基于”，
不能写成自研。

## 更新流程

1. 先运行或取得不可变外部报告；
2. 更新评分卡的结果、日期、commit/run URL 和限制；
3. 再更新本账本、项目案例与简历；
4. 运行 `scripts/check_public_repo.py`，确保链接和禁止过时声明的仓库合同通过；
5. 如果结果回退，简历数字也必须回退，不能只保留历史最好成绩。
