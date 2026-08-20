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
