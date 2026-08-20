from __future__ import annotations

import argparse
import json
import sys
from typing import Any

_PROTOCOL_VERSION = "2024-11-05"


# 构造与请求 id 对应的 JSON-RPC 成功响应
def _success(request_id: object, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


# 构造与请求 id 对应的 JSON-RPC 方法不存在错误
def _method_not_found(request_id: object, method: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


# 处理最小 MCP initialize、工具发现和 echo 调用
def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    method = str(request.get("method", ""))
    request_id = request.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        return _success(
            request_id,
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "coderook-echo-example", "version": "1.0.0"},
            },
        )
    if method == "tools/list":
        return _success(
            request_id,
            {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Return the supplied text without modification",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                            "additionalProperties": False,
                        },
                    }
                ]
            },
        )
    if method == "tools/call":
        params = request.get("params", {})
        arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
        text = str(arguments.get("text", "")) if isinstance(arguments, dict) else ""
        return _success(request_id, {"content": [{"type": "text", "text": text}]})
    return _method_not_found(request_id, method)


# 从 stdin 逐行处理 MCP JSON-RPC 并把响应刷新到 stdout
def serve() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = handle_request(request) if isinstance(request, dict) else None
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


# 在不启动子进程时验证示例的最小协议响应
def self_test() -> None:
    initialized = handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    called = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "hello"}},
        }
    )
    assert initialized is not None
    assert initialized["result"]["protocolVersion"] == _PROTOCOL_VERSION
    assert called is not None
    assert called["result"]["content"][0]["text"] == "hello"
    print("MCP echo self-test passed.")


# 解析自检开关并启动最小 MCP server
def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal CodeRook MCP echo server")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    serve()


if __name__ == "__main__":
    main()
