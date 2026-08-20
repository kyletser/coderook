#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from code_rook.core.mcp.client import McpClient
from code_rook.core.processes import ProcessSupervisor

_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE = _ROOT / "tests" / "fixtures" / "mcp_official_server.py"
_SDK_SPEC = "mcp[cli]==2.0.0"
_TRANSPORTS = ("stdio", "sse", "streamable-http")


@dataclass(frozen=True)
class _ExerciseResult:
    tools: bool
    resources: bool
    prompts: bool
    cancellation: bool


# 向操作系统申请一个临时 loopback 端口
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# 等待官方 HTTP server 开始监听或提前退出
async def _wait_for_server(port: int, process: asyncio.subprocess.Process) -> None:
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if process.returncode is not None:
            raise RuntimeError(f"official MCP server exited with {process.returncode}")
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise TimeoutError("official MCP server did not listen within 15 seconds")


# 等待官方 slow tool 确认收到 cancellation
async def _wait_for_marker(marker: Path) -> bool:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if marker.is_file() and marker.read_text(encoding="utf-8") == "cancelled\n":
            return True
        await asyncio.sleep(0.05)
    return False


# 返回 uvx 命令及固定官方 SDK fixture 参数
def _server_command(transport: str, port: int | None = None) -> tuple[str, list[str]]:
    uvx = shutil.which("uvx")
    if uvx is None:
        raise RuntimeError("uvx is required to run the official MCP SDK fixture")
    args = [
        "--quiet",
        "--from",
        _SDK_SPEC,
        "python",
        str(_FIXTURE),
        "--transport",
        transport,
    ]
    if port is not None:
        args.extend(["--host", "127.0.0.1", "--port", str(port)])
    return uvx, args


# 连接指定官方 transport，stdio 会由客户端自行拥有 server 子进程
async def _connect(
    transport: str,
    *,
    port: int | None,
    marker: Path,
) -> McpClient:
    client = McpClient()
    if transport == "stdio":
        command, args = _server_command(transport)
        await client.connect_stdio(
            command,
            args,
            env={"CODEROOK_MCP_CANCEL_MARKER": str(marker)},
        )
    elif transport == "sse":
        assert port is not None
        await client.connect_sse(f"http://127.0.0.1:{port}/sse")
    else:
        assert port is not None
        await client.connect_streamable_http(f"http://127.0.0.1:{port}/mcp")
    return client


# 对单个连接验证 tools、resources、prompts 与取消通知
async def _exercise(client: McpClient, marker: Path) -> _ExerciseResult:
    tools = await client.list_tools()
    tool_names = {tool.name for tool in tools}
    tools_ok = {"official_echo", "official_slow"}.issubset(tool_names)
    tools_ok = tools_ok and await client.call_tool(
        "official_echo", {"text": "coderook"}
    ) == "official:coderook"

    resources = await client.list_resources()
    resource_uris = {resource.uri for resource in resources}
    contents = await client.read_resource("memory://coderook/guide")
    resources_ok = "memory://coderook/guide" in resource_uris
    resources_ok = resources_ok and any(
        item.get("text") == "official MCP resource" for item in contents
    )

    prompts = await client.list_prompts()
    prompt_names = {prompt.name for prompt in prompts}
    prompt = await client.get_prompt("official_review", {"path": "src/app.py"})
    prompts_ok = "official_review" in prompt_names and "messages" in prompt

    marker.unlink(missing_ok=True)
    slow = asyncio.create_task(
        client.call_tool("official_slow", {"delay_s": 30.0})
    )
    await asyncio.sleep(0.2)
    slow.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await slow
    cancellation_ok = await _wait_for_marker(marker)
    return _ExerciseResult(
        tools=tools_ok,
        resources=resources_ok,
        prompts=prompts_ok,
        cancellation=cancellation_ok,
    )


# 运行一个 transport，关闭首连接后重新握手验证 reconnect
async def _run_transport(transport: str, temp_root: Path) -> dict[str, Any]:
    started = time.monotonic()
    marker = temp_root / f"{transport}-cancelled.txt"
    supervisor = ProcessSupervisor()
    process: asyncio.subprocess.Process | None = None
    port: int | None = None
    client: McpClient | None = None
    reconnect: McpClient | None = None
    try:
        if transport != "stdio":
            port = _free_port()
            command, args = _server_command(transport, port)
            env = {**os.environ, "CODEROOK_MCP_CANCEL_MARKER": str(marker)}
            process = await supervisor.start_exec(
                command,
                *args,
                label=f"mcp-official:{transport}",
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await _wait_for_server(port, process)
        client = await _connect(transport, port=port, marker=marker)
        exercised = await _exercise(client, marker)
        await client.close()
        client = None

        reconnect = await _connect(transport, port=port, marker=marker)
        reconnect_tools = {tool.name for tool in await reconnect.list_tools()}
        reconnect_ok = "official_echo" in reconnect_tools
        passed = all(
            (
                exercised.tools,
                exercised.resources,
                exercised.prompts,
                exercised.cancellation,
                reconnect_ok,
            )
        )
        return {
            "transport": transport,
            "status": "passed" if passed else "failed",
            "tools": exercised.tools,
            "resources": exercised.resources,
            "prompts": exercised.prompts,
            "cancellation": exercised.cancellation,
            "reconnect": reconnect_ok,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        return {
            "transport": transport,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        if client is not None:
            await client.close()
        if reconnect is not None:
            await reconnect.close()
        if process is not None:
            await supervisor.terminate(process)


# 返回当前 Git commit，非仓库环境回退 unknown
def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


# 把机器可读报告渲染为便于代码评审的 Markdown 矩阵
def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Official MCP SDK compatibility report",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Commit: `{report['commit']}`",
        f"- Official SDK: `{report['official_sdk']}`",
        f"- Platform: `{report['platform']}`",
        "",
        "| Transport | Status | Tools | Resources | Prompts | Cancel | Reconnect |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for result in report["results"]:
        lines.append(
            "| {transport} | {status} | {tools} | {resources} | {prompts} | "
            "{cancellation} | {reconnect} |".format(
                transport=result["transport"],
                status=result["status"],
                tools="yes" if result.get("tools") else "no",
                resources="yes" if result.get("resources") else "no",
                prompts="yes" if result.get("prompts") else "no",
                cancellation="yes" if result.get("cancellation") else "no",
                reconnect="yes" if result.get("reconnect") else "no",
            )
        )
    lines.extend(
        [
            "",
            "This report validates CodeRook interoperability with a pinned official SDK "
            "fixture. It does not certify arbitrary third-party MCP servers.",
            "",
        ]
    )
    return "\n".join(lines)


# 运行选定 transport 并写出 JSON/Markdown 互操作证据
async def _run(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="coderook-mcp-interop-") as raw_temp:
        temp_root = Path(raw_temp)
        results = [
            await _run_transport(transport, temp_root)
            for transport in args.transports
        ]
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "commit": _git_commit(),
        "official_sdk": _SDK_SPEC,
        "platform": platform.system().lower(),
        "results": results,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "mcp-official-interop.json"
    markdown_path = args.output_dir / "mcp-official-interop.md"
    json_path.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    markdown_path.write_text(
        _render_markdown(report),
        encoding="utf-8",
        newline="\n",
    )
    print(f"MCP interoperability report: {markdown_path}")
    return 0 if all(result["status"] == "passed" for result in results) else 1


# 解析 transport 与输出目录后启动官方互操作矩阵
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run CodeRook against the pinned official MCP SDK server."
    )
    parser.add_argument(
        "--transports",
        nargs="+",
        choices=_TRANSPORTS,
        default=list(_TRANSPORTS),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".interop-results") / "mcp",
    )
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
