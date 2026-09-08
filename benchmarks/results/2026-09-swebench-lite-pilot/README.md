# SWE-bench Lite：固定 5 题 Pilot（2026-09-07）

## 结论与使用边界

使用真实 CodeRook AgentRunner、Qwen3.8-Flash 和官方 SWE-bench Harness，固定 5 题各解题一次：
**官方 resolved 为 3/5；运行正常结束且官方通过为 2/5。**

这是本地小样本产品 Pilot，不是完整 Lite 300 题成绩，也没有提交官方榜单。
不要在简历上将它简写成“SWE-bench 准确率 60%”，不能据此宣称生产级或优于其他 Agent。
Requests 实例的标准补丁也存在环境回归失败，见下文；保留它在分母中，不剔除失败题。

## 逐题结果

| 实例 | 官方 resolved | CodeRook 结束状态 | 输入 Token | 输出 Token | 解题耗时 | 工具错误 / 调用 |
|---|---|---|---:|---:|---:|---:|
| django__django-15320 | 通过 | 40 步耗尽 | 719,636 | 28,015 | 367.8 s | 6 / 43 |
| psf__requests-3362 | 未通过 | 正常结束 | 600,890 | 27,425 | 349.1 s | 13 / 44 |
| matplotlib__matplotlib-23964 | 通过 | 正常结束 | 860,308 | 19,290 | 357.7 s | 15 / 46 |
| pallets__flask-5063 | 未通过 | 40 步耗尽 | 674,705 | 56,972 | 605.2 s | 13 / 46 |
| scikit-learn__scikit-learn-10297 | 通过 | 正常结束 | 303,262 | 5,949 | 106.4 s | 5 / 26 |

合计输入 3,158,801 Token，输出 137,651 Token，中位解题耗时 357.7 秒。
Token 来自运行时收到的真实 Provider usage；不把累计输入 Token 当作单次上下文长度或账单金额。
这些数字不含镜像下载、官方测试、环境 smoke 及两次 Provider Doctor 连接探针。
模型价格未可靠登记，美元费用保留为未知。

## 可复核证据

- [summary.json](summary.json)：逐题运行状态、usage、工具错误和官方结果。
- [official-summary.json](official-summary.json)：官方 Harness 的整批汇总，5 题均完成判分，执行错误 0，未停止容器 0。
- [reports/](reports/)：五份官方逐题报告，包含 FAIL_TO_PASS / PASS_TO_PASS 结果。
- [predictions.jsonl](predictions.jsonl)：五份 Agent 原始补丁，包括失败补丁。
- [manifest.json](manifest.json)：预先冻结的 Pilot 及独立 20 题名单、数据版本和选择方法。
- [execution-contract.json](execution-contract.json)：脱敏候选配置、源码和适配器指纹、逐题镜像与补丁摘要。

完整本地事件、Session Ledger、运行日志、请求快照及环境控制日志存于被 Git 忽略的
`.benchmark-results/swebench-lite-20260907/`。它们没有直接公开，避免泄露凭据、完整会话或本机路径。

## 固定条件

- Runtime 源码 commit：`7cda2be6bc07b45845f9fd9bf196db2b1593e25f`。
- 解题镜像：`sha256:80760f56151d6498226675927ee0d324d4379d1894fd9b9ebc536aa80d8c61ee`。
- 解题时适配器 SHA-256：`d7eebc44e4e552536a0bacece39ba8b183da4f8ca6392db08b044f2934331792`。
- 解题时调度脚本 SHA-256：`a66bd454f9a4f6ed35b03edd7172179ed763d9a7f00c35580f119d968f3974c2`。
- 数据：`SWE-bench/SWE-bench_Lite`，revision `b0dde1093fe417d83b7184254edf8199c1f0dff5`。
- Parquet SHA-256：`438e281d80587aa7be470896ce410557002fde02d2ceee3e099331d308f62dd3`。
- 按仓库与实例 ID 的固定哈希顺序轮转选择；不使用 patch、测试结果或人工难度挑题。
- Qwen3.8-Flash，OpenAI Chat wire format，temperature 0，单 Agent / direct。
- 每题最多 40 步、900 秒；解题恰好一次，没有人工补改 Agent 补丁。
- Windows Docker Desktop，Linux x86_64 实例；每个 Agent 容器上限 4 CPU / 6 GiB。
- SWE-bench Harness 5.0.2；最终整批判分 run ID：`coderook-lite-pilot-1788756068`。

Agent 仅收到 Issue、仓库标识和基线 commit，不接触 gold patch、test patch、评测数据挂载或 Docker socket。
官方镜像部分含仅调整文件权限的准备提交，因此比较基线与镜像 HEAD 的逐文件对象及路径，并从镜像 HEAD 导出改动，避免把预置权限差异导出为模型补丁。

模型解题结束后，仅修改了汇总诊断字段和评测容器依赖；没有重新生成模型答案。
最初逐题判分及首次整批判分存在清理阶段缺少 Docker CLI 的错误。
补齐 CLI 后对相同五份补丁重新整批判分，结果仍为 3/5，清理错误归零。
临时控制器使用已安装的 Docker CLI 29.1.3；Dockerfile 同步补齐官方 Docker CLI 依赖。

## 环境控制与失败解释

1. **Django**：不改变实现的控制补丁未通过，官方标准补丁通过；Agent 补丁也通过，但运行时步数耗尽。
2. **Requests**：Agent 目标测试未通过；官方标准补丁的目标测试通过，但两项原有超时测试失败。
   日志另有 `recursive dependency involving fixture 'httpbin'` 错误；检查发现官方镜像缺少 `pytest-httpbin` / `httpbin`。
   因而存在真实环境问题，但不能据此替 Agent 的目标测试失败免责，也不能删除该题或改成通过。
3. **Flask**：Agent 的两项目标测试均未通过，54 项原有回归通过；官方标准补丁通过。属于有效的解题失败证据。
4. **Matplotlib / scikit-learn**：Agent 补丁通过官方目标测试和要求保留的回归测试。

没有修改官方测试或断言，没有用标准补丁帮助 Agent 继续解题，也没有选择多次模型生成中的最好结果。

## 扩大评测前的优先级

**本轮结论：Pilot 已完成，正式 20 题暂不启动。预算不是阻断因素。**

1. **先改善工具接口适配**：205 次工具调用出现 52 次错误，其中 41 次是遗漏 action 的 schema 错误。
   应核对模型实际收到的 schema、调用示例和错误反馈，再用 keyless 协议回归与少量真实调用验证；不能靠关闭权限校验解决。
2. **改善定点读取与收尾**：Django 为同一大文件使用了 8 次 Artifact 分页读取；2/5 任务耗尽 40 步。
   优先提供明确的行范围读取，并检查重复探索和结束条件。增加步数只能作为独立候选配置记录，不修改本轮成绩。
3. **补齐 Requests 测试环境**：修复依赖及连接超时环境后，用原始 baseline / 官方标准补丁对照验证；不改变测试、不移除失败实例。
4. **再运行冻结 20 题**：使用修复后统一候选、单独版本指纹和每题一次的规则。Pilot 用于发现问题，不能和后续正式切片混算。

本轮仅运行 7 项相关单元测试、相关脚本 Ruff、容器运行 smoke 和实际官方评测，没有运行全量项目测试或修改 CI。
