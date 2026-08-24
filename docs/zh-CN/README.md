# CodeRook 中文快速开始

> 当前仍是未发布 Alpha，`v1.0.0` 评分卡为 NO-GO。源码安装可用于试用，但公开安装包和真实模型
> Benchmark 尚未发布。

## 从源码启动

需要 Python 3.12、Git 和 [`uv`](https://docs.astral.sh/uv/)：

```bash
git clone https://github.com/kyletser/coderook.git
cd coderook
uv sync
uv run coderook
```

`coderook` 会打开 TUI，并自动启动或复用当前仓库对应的本地 Core。首次进入不会强制配置 API；在
第一次提交 Coding 任务前，readiness 会检查活动 route、凭据或本地模型端口。

## 配置模型

在 TUI 输入 `/config`，或者使用统一 CLI：

```bash
uv run coderook configure
uv run coderook provider list
uv run coderook provider add local --preset ollama --activate
uv run coderook provider test local
```

远端 Provider 的新增、编辑和切换必须通过有界 Doctor 探针。Ollama 与 LM Studio 只探测本机回环
端口，不需要 API Key。仓库 `.env` 不会自动读取；只有显式 `--env-file <path>` 才会加载指定文件。
显式文件禁用变量插值、不修改进程环境，用户进程同名值优先；无法确认已有 Core 使用同一 overlay 时
会安全拒绝复用。

## 完成第一个任务

1. 用 `/plan <任务>` 做只读规划，或直接在 `act` 模式描述任务。
2. 核对权限卡中的命令、路径与 Windows “NO OS SANDBOX” 提示。
3. 在结果卡查看真实执行状态、模型、改动与验证证据。
4. 用 `/changes` 查看文件和 hunk；需要时使用 `/review` 或 `/rewind`。
5. `/stage <path...> --yes` 和 `/commit <主题> --yes` 只操作本地仓库，v1 不自动 push。

完整命令、Goal Loop、Provider、安全边界与故障恢复说明见[中文使用说明](../guides/USER_GUIDE.md)，
当前发布结论见[发布评分卡](../status/RELEASE_SCORECARD.md)。

Fleet、Workflow、Hooks v2、MCP Resources/Prompts 与 VS Code 原型属于默认关闭的 Labs；普通使用无需
开启。维护者只有在明确接受实验性恢复语义时才应在启动前设置 `CODEROOK_LABS=1`，并重启 Core。
