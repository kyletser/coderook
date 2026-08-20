# learn-claude-code 机制移植说明

本轮没有复制参考仓库的教学代码，而是提炼其高价值运行时机制，并按 CodeRook 现有的类型化、异步、双进程边界重新实现。

## 已移植能力

| 机制 | CodeRook 实现 | 工程约束 |
|---|---|---|
| 生命周期 Hooks | `core/hooks/` | 四个异步阶段，回调异常隔离，阻断结果类型化 |
| 项目长期记忆 | `core/memory/`、`memory_*` 工具 | JSON 记录、Markdown 索引、来源追踪、敏感信息脱敏、确定性检索 |
| 上下文恢复 | `core/compact/`、`core/loop.py` | 结构化增量压缩、最近窗口保留、工具结果三级预算、质量门禁和溢出恢复 |
| 后台任务 | `core/background/`、`background_*` 工具 | 由 daemon 持有，跨轮次存活，异步读取和进程树清理 |
| 任务认领 | `task_claim` | 原子认领、阻塞依赖检查、owner/worktree 持久化 |
| Git worktree 隔离 | `core/worktree/`、`worktree_*` 工具 | 固定根目录、名称校验、脏目录删除保护 |
| 子代理工作区 | `spawn_agent(worktree=...)` | 文件、Bash、Git、检查点和边界检查全部绑定到目标 worktree |

## Agent 可调用工具

- `memory_save`、`memory_search`、`memory_forget`
- `background_start`、`background_result`、`background_list`、`background_cancel`
- `task_claim`
- `worktree_create`、`worktree_list`、`worktree_remove`
- `spawn_agent` 新增可选参数 `worktree`

写入记忆、启动或取消后台进程、创建或删除 worktree 都经过现有权限系统。后台任务状态通过类型化事件 `background.started` 和 `background.finished` 进入 IPC 与 TUI。

## 本地验证

```powershell
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run python scripts\gen_protocol_doc.py --check
```

手工体验时先启动两个终端：

```powershell
# 终端 1
uv run coderook-core

# 终端 2
uv run coderook-tui
```

可以依次让 Agent“记住本项目使用 uv”“在后台运行一个短命令并查询结果”“创建一个 worktree 后让子代理在其中工作”，观察权限审批、工具事件和结果回填。

## 有意保留的边界

- 记忆检索采用可解释的中英文词法打分，没有引入向量数据库或 embedding 服务，避免增加部署依赖和隐式网络请求。
- worktree 仅允许位于 `.coderook/worktrees/`，不接受任意路径，防止子代理逃逸项目边界。
- 后台任务属于 daemon 生命周期；daemon 退出时会清理进程树，不把失控进程留在系统中。
- Hooks 是进程内扩展点，当前不执行任意外部脚本；后续可以在权限和配置模型完备后增加声明式 Hook 配置。

## 上下文压缩 V2

- 默认在上下文使用率达到 80% 时触发，保留约 25% 最近消息原文。
- `tool_use` 与 `tool_result` 被视为不可拆分闭环；协议不完整时拒绝替换历史。
- 旧历史由模型输出 JSON，再经 Pydantic 校验为目标、完成项、约束、决策、文件、TODO、错误和关键数据。
- 压缩前检查源历史中的约束、TODO、错误与文件路径是否在摘要中得到覆盖；质量不合格时保持原历史。
- 再次压缩时只合并上一版摘要与新增旧历史，不重复总结完整原始 transcript。
- 工具输出分为原文保留、确定性头尾截断、LLM 蒸馏三级，蒸馏失败自动回退截断。
- TUI 展示触发原因、压缩前后 token、最近窗口消息数、质量分与摘要文件路径。

相关配置：

```toml
[compaction]
auto_threshold = 0.80
retain_ratio = 0.25
tool_result_limit = 8000
tool_result_keep = 4000
tool_result_summarize_threshold = 20000
```
