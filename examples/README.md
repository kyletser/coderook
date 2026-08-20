# CodeRook 可运行示例

这些示例只使用 CodeRook 本身和 Python 标准库。先完成 `coderook configure`，并确保当前目录是要处理的
Git 工作区。

## 1. 只读代码审查

```bash
python examples/read_only_review.py --goal "审查当前改动，按优先级报告真实缺陷并给出文件位置"
```

脚本只给 headless run 放行读取、搜索和 diff 工具。先用 `--print-command` 查看完整命令，不调用模型：

```bash
python examples/read_only_review.py --print-command
```

## 2. 受控自动修复

```bash
python examples/automated_fix.py --goal "修复测试失败，保持改动最小并运行相关测试"
```

该示例显式放行读取、精确编辑、patch、diff、Run 与 Bash；工作区边界和危险命令规则仍然生效。
建议先在干净 Git 分支运行，并在完成后审查 `git diff`。

## 3. 最小 MCP stdio 扩展

`mcp_echo_server.py` 实现一个无第三方依赖的 MCP `echo` 工具。把绝对脚本路径加入用户级
`~/.coderook/config.toml`：

```toml
[[mcp.servers]]
name = "echo-example"
transport = "stdio"
command = "python"
args = ["/absolute/path/to/coderook/examples/mcp_echo_server.py"]
```

重启 Core 后在 TUI 输入 `/mcp`，应看到 `echo-example__echo`。MCP server 属于外部执行边界；真实扩展
应固定来源、审查代码、最小化环境变量并避免把密钥写进参数。

本地协议自检：

```bash
python examples/mcp_echo_server.py --self-test
```

## 4. 项目级 focused-fix Skill

`skills/focused-fix/SKILL.md` 演示一个有界编码流程：先理解仓库，只做最小修改，运行聚焦验证，
最后检查 diff。先预览安装内容，再显式确认并信任这份已审查的本地源码：

```bash
uv run coderook skills install examples/skills/focused-fix --scope project --trust
uv run coderook skills install examples/skills/focused-fix --scope project --trust --yes
uv run coderook skills audit
```

之后在 TUI 输入：

```text
/focused-fix 修复一个具体失败并运行相关测试
```

Skill 正文会进入模型上下文，因此即使来源于本地也应先审查；安装后的 digest 变化会在执行前失败，
但“通过完整性校验”不等于“内容安全”。

## 5. 阻止敏感文件写入的 Hook

该项目级 Hook 在 `File` 写入发生前检查目标，阻止 `.env`、私钥和常见凭据文件。复制示例后再授予
工作区信任；未受信任项目中的 project hook 会被跳过并写入审计事件。

```bash
mkdir -p .coderook/hooks
cp examples/hooks/guard_sensitive_files.py .coderook/hooks/guard_sensitive_files.py
cp examples/hooks/hooks.toml .coderook/hooks.example.toml
uv run python .coderook/hooks/guard_sensitive_files.py --self-test
```

审查 `.coderook/hooks.example.toml` 后，把其中 `[[hooks]]` 块合并进现有
`.coderook/hooks.toml`（不要覆盖已有配置）。在 TUI 中执行 `/trust grant` 后重启 Core，再用
`/hooks` 查看加载与执行审计。示例采用 blocking +
fail-closed：进程失败或超时会拒绝对应工具调用。Hook 是本机子进程执行边界，必须固定命令、限制输出、
避免联网，并把 stdin 当作已脱敏但仍然敏感的任务元数据。
