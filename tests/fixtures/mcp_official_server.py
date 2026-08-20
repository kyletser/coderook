from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from mcp.server import MCPServer

server = MCPServer(
    "coderook-official-sdk-fixture",
    version="1.0.0",
    instructions="Deterministic CodeRook interoperability fixture.",
)


@server.tool()
# 回显输入文本以验证工具发现、参数 schema 与 content 解码
async def official_echo(text: str) -> str:
    return f"official:{text}"


@server.tool()
# 挂起工具直到完成或收到 MCP cancellation，并用脱敏 marker 记录取消
async def official_slow(delay_s: float = 30.0) -> str:
    try:
        await asyncio.sleep(delay_s)
    except asyncio.CancelledError:
        marker = os.environ.get("CODEROOK_MCP_CANCEL_MARKER", "")
        if marker:
            Path(marker).write_text("cancelled\n", encoding="utf-8", newline="\n")
        raise
    return "completed"


@server.resource("memory://coderook/guide", mime_type="text/plain")
# 返回固定资源内容以验证 resource 不会被错误包装成 tool
async def official_guide() -> str:
    return "official MCP resource"


@server.prompt()
# 渲染带参数的固定 prompt 以验证 prompts/list 与 prompts/get
async def official_review(path: str) -> str:
    return f"Review {path} without writing files."


# 解析 transport/host/port 并启动官方 SDK server
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        required=True,
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.transport == "stdio":
        server.run(transport="stdio")
    elif args.transport == "sse":
        server.run(
            transport="sse",
            host=args.host,
            port=args.port,
            sse_path="/sse",
            message_path="/messages/",
        )
    else:
        server.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path="/mcp",
        )


if __name__ == "__main__":
    main()
