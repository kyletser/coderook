from __future__ import annotations

from code_rook.tui.panels.manage import (
    render_hooks,
    render_job_output,
    render_jobs,
    render_mcp_servers,
    render_mcp_tools,
    render_memory,
    render_workers_summary,
)


# 功能：MCP server 列表展示名称/传输/状态/工具数，空列表给出提示
# 设计：不依赖 Textual 生命周期，直接注入 dict 数据验证紧凑投影与空态分支
def test_render_mcp_servers_lists_servers() -> None:
    rendered = render_mcp_servers(
        [
            {
                "name": "github",
                "transport": "stdio",
                "status": "connected",
                "tool_count": 3,
                "error": "",
            },
            {
                "name": "broken",
                "transport": "sse",
                "status": "failed",
                "tool_count": 0,
                "error": "token revoked",
            },
        ]
    )

    assert "[bold cyan]MCP servers[/bold cyan]" in rendered
    assert "github" in rendered
    assert "stdio" in rendered
    assert "connected" in rendered
    assert "3 tool(s)" in rendered
    assert "token revoked" in rendered
    assert "/mcp <name>" in rendered


# 功能：无 MCP server 时给出明确空态提示
# 设计：覆盖空列表分支，避免 className/existence 依赖假路径
def test_render_mcp_servers_empty() -> None:
    assert "没有配置 MCP server" in render_mcp_servers([])


# 功能：MCP 工具清单展开展示工具名与描述
# 设计：注入含 name/description 的工具 dict，验证逐条渲染而非只总结数量
def test_render_mcp_tools_lists_tools() -> None:
    rendered = render_mcp_tools(
        {
            "name": "github",
            "status": "connected",
            "tools": [
                {"name": "create_repo", "description": "create a repo"},
                {"name": "delete_repo", "description": ""},
            ],
        }
    )

    assert "MCP github" in rendered
    assert "create_repo" in rendered
    assert "create a repo" in rendered
    assert "delete_repo" in rendered


# 功能：无工具的 server 给出「未发现工具」提示
# 设计：tools 为空列表时不应抛错，应给出可读的占位说明
def test_render_mcp_tools_no_tools() -> None:
    rendered = render_mcp_tools({"name": "x", "status": "connected", "tools": []})
    assert "未发现工具" in rendered


# 功能：hook 面板展示配置表与最近执行记录
# 设计：configs 含命令展开、audit 含状态/耗时的成败徽标，验证结构化排版
def test_render_hooks_configs_and_audit() -> None:
    rendered = render_hooks(
        {
            "configs": [
                {
                    "id": "post-commit",
                    "event": "session.created",
                    "blocking": True,
                    "trusted_scope": "local",
                    "command": ["python", "notify.py"],
                }
            ],
            "audit_events": [
                {
                    "hook_id": "post-commit",
                    "status": "completed",
                    "elapsed_ms": 12,
                    "ts": "2026-08-17T10:00:00",
                    "reason": "",
                }
            ],
        }
    )

    assert "post-commit" in rendered
    assert "session.created" in rendered
    assert "block" in rendered
    assert "$ python notify.py" in rendered
    assert "12ms" in rendered


# 功能：记忆面板列出 id/name/type 并带删除提示
# 设计：注入多类型条目，验证按条目渲染并保留固定提示尾巴
def test_render_memory_lists_entries() -> None:
    rendered = render_memory(
        [
            {"id": "m1", "name": "arch", "type": "app", "description": "layout"},
            {"id": "m2", "name": "style", "type": "style", "description": ""},
        ]
    )

    assert "m1" in rendered
    assert "arch" in rendered
    assert "app" in rendered
    assert "/memory delete" in rendered


# 功能：无记忆条目时给出空态提示
# 设计：覆盖空列表分支
def test_render_memory_empty() -> None:
    assert "没有记忆条目" in render_memory([])


# 功能：后台任务中心展示任务 ID/状态/命令与输出预览
# 设计：注入带 output 的任务验证增量预览截断，且带取消提示
def test_render_jobs_lists_jobs() -> None:
    rendered = render_jobs(
        [
            {
                "id": "j1",
                "status": "running",
                "command": "uv run pytest",
                "output": "collected 3 items\nrunning...\n",
            }
        ]
    )

    assert "j1" in rendered
    assert "uv run pytest" in rendered
    assert "collected 3 items" in rendered
    assert "/jobs cancel" in rendered


# 功能：单任务全部输出视图包含终态标签
# 设计：job 数组取首项，验证完整 output 透传而非预览截断
def test_render_job_output_full() -> None:
    rendered = render_job_output(
        [
            {
                "id": "j1",
                "status": "completed",
                "command": "uv run ruff check",
                "output": "All checks passed!",
            }
        ]
    )

    assert "Job j1" in rendered
    assert "All checks passed!" in rendered
    assert "$ uv run ruff check" in rendered


# 功能：单任务输出视图在 jobs 为空时提示未找到
# 设计：覆盖空数组分支，避免空索引抛错
def test_render_job_output_missing() -> None:
    assert "未找到该任务" in render_job_output([])


# 功能：并行子代理结果统一汇总折叠为一行一结果
# 设计：注入 worker_id/status/summary，验证多行 summary 被折叠并带取消提示
def test_render_workers_summary_flat() -> None:
    rendered = render_workers_summary(
        [
            {
                "worker_id": "w1",
                "description": "compile",
                "status": "completed",
                "summary": "ok\nmore",
            }
        ]
    )

    assert "w1" in rendered
    assert "compile" in rendered
    assert "ok" in rendered
    assert "more" not in rendered
    assert "/jobs cancel" in rendered


# 功能：无并行子代理时给出空态提示
# 设计：覆盖空列表分支
def test_render_workers_summary_empty() -> None:
    assert "没有并行子代理" in render_workers_summary([])