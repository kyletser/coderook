from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class _BlockingModelHandler(BaseHTTPRequestHandler):
    request_started = threading.Condition()
    request_count = 0

    # 接收 OpenAI-compatible 请求并短暂阻塞，为 daemon 强杀提供稳定窗口
    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)
        with self.request_started:
            type(self).request_count += 1
            self.request_started.notify_all()
        time.sleep(3.0)
        payload = json.dumps({"error": "delayed crash-matrix model"}).encode("utf-8")
        try:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # 禁止测试服务器把请求或 Authorization header 写入控制台
    def log_message(self, format: str, *args: object) -> None:
        return


# 分配当前进程尚未占用的 loopback TCP 端口
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


# 启动只在 loopback 监听的阻塞模型服务器
def _start_model_server() -> tuple[ThreadingHTTPServer, threading.Thread, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BlockingModelHandler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


# 构造隔离 HOME、随机端口和本地假模型所需的 daemon 环境
def _daemon_environment(home: Path, ipc_port: int, api_port: int, model_port: int) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CODEROOK_PORT": str(ipc_port),
            "CODEROOK_API_PORT": str(api_port),
            "CODEROOK_LOG_FILE": "",
            "CODEROOK_LOG_LEVEL": "WARNING",
            "CODEROOK_LLM_PROVIDER": "openai_compatible",
            "CODEROOK_LLM_DEFAULT_MODEL": "crash-matrix-model",
            "CODEROOK_LLM_BASE_URL": (
                f"http://127.0.0.1:{model_port}/v1/chat/completions"
            ),
            "CODEROOK_LLM_API_KEY_ENV": "CODEROOK_MATRIX_KEY",
            "CODEROOK_MATRIX_KEY": "test-only-not-a-real-key",
        }
    )
    return env


# 启动真实 daemon 并以鉴权 capabilities 请求确认 HTTP API 已可服务
def _start_daemon(env: dict[str, str], home: Path, api_port: int) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [sys.executable, "-m", "code_rook.core"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    token_path = home / ".coderook" / "api-token"
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"daemon exited during startup: {process.returncode}")
        if token_path.is_file():
            try:
                token = token_path.read_text(encoding="utf-8").strip()
                _request_json(api_port, token, "GET", "/v1/capabilities")
                return process
            except (OSError, RuntimeError, UnicodeError, urllib.error.URLError):
                pass
        time.sleep(0.05)
    process.kill()
    process.wait()
    raise RuntimeError("daemon did not become ready within 15 seconds")


# 调用 runtime HTTP API 并返回 JSON 对象；重启后的只读查询允许短暂连接拒绝重试
def _request_json(
    api_port: int,
    token: str,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{api_port}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    deadline = time.monotonic() + 5.0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=5.0) as response:
                payload = json.loads(response.read())
            break
        except (ConnectionError, OSError, urllib.error.URLError):
            if method != "GET" or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    if not isinstance(payload, dict):
        raise RuntimeError(f"API returned non-object payload for {path}")
    return payload


# 等待阻塞模型确认收到本轮请求，确保强杀发生在活动 turn 内
def _wait_for_model_request(expected_count: int) -> None:
    deadline = time.monotonic() + 10.0
    with _BlockingModelHandler.request_started:
        while _BlockingModelHandler.request_count < expected_count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("model request did not arrive within 10 seconds")
            _BlockingModelHandler.request_started.wait(timeout=remaining)


# 强制终止 daemon 并等待 OS 回收，模拟电源中断或进程强杀
def _hard_kill(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5.0)


# 执行真实 daemon 活动 turn 强杀/重启循环并输出逐轮 receipt 证据
def run_matrix(iterations: int, output: Path) -> dict[str, Any]:
    ipc_port = _free_port()
    api_port = _free_port()
    while api_port == ipc_port:
        api_port = _free_port()
    model_server, model_thread, model_port = _start_model_server()
    with _BlockingModelHandler.request_started:
        _BlockingModelHandler.request_count = 0
    results: list[dict[str, Any]] = []
    process: subprocess.Popen[bytes] | None = None
    temp_root: Path | None = None
    infrastructure_error: str | None = None
    report: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(
            prefix="coderook-crash-matrix-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            temp_root = Path(temp_dir).resolve()
            home = Path(temp_dir) / "home"
            home.mkdir()
            env = _daemon_environment(home, ipc_port, api_port, model_port)
            process = _start_daemon(env, home, api_port)
            token = (home / ".coderook" / "api-token").read_text(
                encoding="utf-8"
            ).strip()
            thread = _request_json(
                api_port,
                token,
                "POST",
                "/v1/threads",
                {"title": "crash matrix", "mode": "chat"},
            )
            thread_id = str(thread["id"])
            for index in range(iterations):
                turn = _request_json(
                    api_port,
                    token,
                    "POST",
                    f"/v1/threads/{thread_id}/turns",
                    {"content": f"crash injection {index + 1}", "mode": "act"},
                )
                turn_id = str(turn["id"])
                _wait_for_model_request(index + 1)
                _hard_kill(process)
                process = _start_daemon(env, home, api_port)
                recovered = _request_json(
                    api_port,
                    token,
                    "GET",
                    f"/v1/turns/{turn_id}",
                )
                receipt = _request_json(
                    api_port,
                    token,
                    "GET",
                    f"/v1/turns/{turn_id}/receipt",
                )
                status = str(recovered.get("status", ""))
                error = dict(recovered.get("error") or {})
                passed = (
                    status == "interrupted"
                    and error.get("reason") == "daemon_restarted"
                    and receipt.get("status") == "interrupted"
                    and receipt.get("finished_at") is not None
                )
                results.append(
                    {
                        "iteration": index + 1,
                        "turn_id": turn_id,
                        "status": status,
                        "reason": error.get("reason"),
                        "receipt_status": receipt.get("status"),
                        "passed": passed,
                    }
                )
            process.terminate()
            process.wait(timeout=5.0)
            process = None
    except Exception as exc:
        infrastructure_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                _hard_kill(process)
        model_server.shutdown()
        model_server.server_close()
        model_thread.join(timeout=5.0)
        if temp_root is not None and temp_root.name.startswith("coderook-crash-matrix-"):
            shutil.rmtree(temp_root, ignore_errors=True)
        passed = sum(bool(item["passed"]) for item in results)
        report = {
            "schema_version": 2,
            "generated_at": datetime.now(UTC).isoformat(),
            "iterations": iterations,
            "completed_iterations": len(results),
            "passed": passed,
            "recovery_rate": passed / iterations if iterations else 0.0,
            "infrastructure_error": infrastructure_error,
            "coverage": [
                "user_message_durable",
                "llm_request_in_flight",
                "client_disconnect",
                "daemon_hard_kill",
                "restart_reconcile",
                "receipt_rebuild",
            ],
            "results": results,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return report


# 解析候选门禁参数，默认执行计划要求的 100 次故障注入
def main() -> int:
    parser = argparse.ArgumentParser(description="Run the daemon crash recovery matrix")
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--min-rate", type=float, default=0.95)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/crash-recovery.json"),
    )
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    if not 0.0 <= args.min_rate <= 1.0:
        parser.error("--min-rate must be between 0 and 1")
    try:
        report = run_matrix(args.iterations, args.output)
    except (OSError, RuntimeError, urllib.error.URLError) as exc:
        print(f"crash recovery matrix failed: {exc}", file=sys.stderr)
        return 2
    rate = float(report["recovery_rate"])
    print(
        f"crash recovery: {report['passed']}/{report['iterations']} "
        f"({rate:.1%}); evidence={args.output}"
    )
    return 0 if rate >= args.min_rate else 1


if __name__ == "__main__":
    raise SystemExit(main())
