# SWE-bench Lite：修复后的五题开发回归

这不是独立测试集或完整 SWE-bench Lite 榜单成绩。使用与[首次 Pilot](../2026-09-swebench-lite-pilot/README.md)
相同的五个实例、Qwen3.8-Flash、temperature=0、单 Agent/direct、每题 40 步及 900 秒限制。
每题只运行一次真实模型，未人工修改预测补丁；另外二十个冻结实例没有运行。

## 结论

工具接口修复有效，但整体解题能力和可靠完成率没有提升：官方通过仍为 **3/5**，正常结束且通过仍为
**2/5**。Flask 的补丁由失败变为通过，Django 则由通过退化为超时、空补丁。不能挑选改善样本宣传全面提升。

| 指标 | 首次 Pilot | 本次回归 |
|---|---:|---:|
| 官方 resolved / 全部提交实例 | 3/5 | 3/5 |
| 正常结束且官方通过 | 2/5 | 2/5 |
| 工具调用数 | 205 | 163 |
| 工具失败数 | 52 | 17 |
| schema_error 事件数 | 41 | 2 |
| 缺少 action 的错误数 | 41 | 0 |
| 已记录输入 Token | 3,158,801 | 1,828,554 |
| 已记录输出 Token | 137,651 | 179,388 |
| 单题 Agent 耗时中位数 | 357.7 秒 | 292.1 秒 |
| 五题 Agent 耗时合计 | 1,786.2 秒 | 1,938.2 秒 |

已记录输入 Token 减少 42.1%，输出 Token 增加 30.3%；中位耗时降低 18.3%，但总耗时增加约 8.5%。
这些是同五题、每配置一次的描述性统计，不具备泛化或显著性保证，也不是单一功能消融。
模型价格未核验，美元费用未知，不能将输入 Token 降幅写成费用降幅。Django 超时及 Flask 传输中断的
最后一次请求可能没有返回完整 usage；上述 Token 是已收到的 usage 合计，不是完整账单核对值。

## 逐题结果

| 实例 | 首次官方结果 | 本次官方结果 | 本次 Runtime | 本次输入 / 输出 Token | Agent 耗时 |
|---|---|---|---|---:|---:|
| django__django-15320 | 通过 | 空补丁，未通过 | wall_time_exceeded | 630,576 / 91,013 | 900.1 秒 |
| psf__requests-3362 | 未通过 | 未通过 | success | 420,815 / 22,616 | 292.1 秒 |
| matplotlib__matplotlib-23964 | 通过 | 通过 | success | 121,365 / 2,954 | 79.0 秒 |
| pallets__flask-5063 | 未通过 | 通过 | llm_error | 333,287 / 56,592 | 566.1 秒 |
| scikit-learn__scikit-learn-10297 | 通过 | 通过 | success | 322,511 / 6,213 | 101.0 秒 |

官方 Harness 对空补丁不运行实例测试，因此 Django 没有本轮实例 `report.json`，本地 summary 将它
标为 `ungraded`，官方总报告标为 `empty_patch`。它始终保留在五题分母内，不能删掉后改写为 3/4。

Requests 的目标测试 `test_response_decode_unicode` 本次仍失败；两项 PASS_TO_PASS 超时测试也失败。
此前参考补丁对照及本轮镜像核查表明，原镜像缺少 `httpbin`、`pytest-httpbin`，pytest 版本与仓库
requirements 不同；此前参考补丁也无法通过两项超时测试。为保持前后可比，本轮没有替换实例依赖。
**Agent 的目标测试失败不能全部归因于环境，不删除该失败，也不将参考补丁结果替代 Agent 结果。**

Flask 的目标和回归测试通过，但 Agent 在第 27 步遇到 OpenAI-compatible HTTP 传输异常，运行时正确
记录失败。当前 Provider 将该异常折叠为通用 `RuntimeError`，日志未保留具体 HTTPError 子类型；不能
进一步断言是服务端、DNS、TLS 还是代理故障。Loop 的瞬态错误识别没有触发续试，最终补丁由官方独立验收。

## 本轮证明和暴露了什么

- 模型省略 `Bash.run` 的 action 不再造成失败，原审批与执行管线仍保留。
- 其余接口问题仍存在：未知 `Read/file` 工具名、超出 120 秒的 Bash timeout、缺少 edit.old_text，
  以及 unified diff hunk 长度错误。schema_error 只是一种事件分类，不能代表全部工具参数或构造错误。
- Django 长时间探索而不修改代码，最终达到墙钟限制；仅在最后三个模型步骤提示收尾不够。
- Runtime 成功不等于代码正确（Requests）；代码正确也不等于正常完成（Flask）。两个指标必须同时报告。
- 按行读取、提示词、缺省 action 和 Repository 可见性一同变化，不能把 Token 变化单独归因于按行读取。

在处理任务收尾与模型传输恢复、冻结下一候选前，不扩大到二十题，也不将这些数据宣称为生产级可靠性。

## 冻结条件与证据

- 数据集及五题选择完全复用首次 Pilot 的冻结文件；没有根据结果重新选题。
- 基础提交：`7cda2be6bc07b45845f9fd9bf196db2b1593e25f`，**另有未提交修复**，不是仅运行该提交。
- 修复后源码归档 SHA-256：`fe1f4fe237cb04514e275c631b502a8408c0133ae09ee171a623d5c9bef9e79e`。
- Runtime 镜像：`sha256:6dfc7a9c913f1615ca4d93e943a4e6ceb8f74d28b88de08307eee1875cd602de`。
- 官方 Harness：`swebench==5.0.2`；Windows Docker Desktop / Linux x86_64。
- 官方判分 run：`coderook-lite-pilot-1788760400`；5 题提交，4 题执行，1 题空补丁，0 个 Harness
  error instance、0 个残留实例容器。镜像缓存保留。
- 重建遇到 PyPI TLS handshake EOF，故复用已安装且未变化的依赖，只复制修复后源码与 adapter；
  基础镜像指纹、构建方式和源码快照指纹见 [candidate.json](candidate.json)。
- 新镜像先完成不调用模型的真实 Bash 烟测，以及 Django 未修复基线失败、参考补丁通过的官方环境对照。

完整源码归档、工作树 diff、请求快照、事件、日志和 preflight 证据保存在本机忽略目录
`.benchmark-results/swebench-lite-retest-20260907/`，公开文件不包含完整会话或凭据。
仅检出上述基础提交不能复现候选；必须同时使用对应源码修复与冻结 adapter。这份回归不是干净提交的
正式发布证据，之后正式切片仍需绑定冻结候选。

- [前后逐题对比](comparison.json)
- [本次汇总](summary.json)
- [官方汇总（包含空补丁 ID）](official-summary.json)
- [五份原始预测补丁](predictions.jsonl)
- [运行参数契约](execution-contract.json)
- [四份官方实例报告](reports/)

本轮没有重新尝试模型解题、扩大样本、修改官方测试、提交或推送代码，也没有运行全量项目测试。
