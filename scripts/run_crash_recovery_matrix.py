from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import sqlite3
import statistics
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

_READ_RETRY_TIMEOUT_S = 30.0
_MODEL_REQUEST_TIMEOUT_S = 30.0
_RETRYABLE_READ_STATUSES = frozenset({404, 409, 500, 502, 503, 504})


class _BlockingModelHandler(BaseHTTPRequestHandler):
    request_started = threading.Condition()
    request_count = 0
    request_phase = "llm_request_in_flight"
    phase_request_count = 0
    shell_command = ""

    # 按故障阶段阻塞模型请求或返回一个待恢复的工具调用
    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)
        with self.request_started:
            type(self).request_count += 1
            type(self).phase_request_count += 1
            phase = type(self).request_phase
            phase_request_count = type(self).phase_request_count
            self.request_started.notify_all()
        response: dict[str, object]
        if phase == "request_snapshot_in_flight" or (
            phase == "tool_result_persisted" and phase_request_count > 1
        ):
            time.sleep(3.0)
            status = 503
            response = {"error": "delayed crash-matrix model"}
        else:
            tool_name = "Bash"
            arguments: dict[str, object] = {
                "action": "run",
                "command": "echo crash-matrix",
            }
            if phase == "shell_process_running":
                arguments["command"] = type(self).shell_command
            elif phase == "tool_result_persisted":
                tool_name = "File"
                arguments = {
                    "action": "write",
                    "path": "recovery-marker.txt",
                    "content": "written-once\n",
                }
            status = 200
            response = {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "crash-matrix-tool-call",
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        payload = json.dumps(response).encode("utf-8")
        try:
            self.send_response(status)
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
def _daemon_environment(
    home: Path, ipc_port: int, api_port: int, model_port: int
) -> dict[str, str]:
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
            "CODEROOK_LLM_BASE_URL": (f"http://127.0.0.1:{model_port}/v1/chat/completions"),
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
        cwd=home,
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


# 把 HTTP 错误转换为包含方法、路径、状态和脱敏响应摘要的稳定诊断
def _http_error(method: str, path: str, exc: urllib.error.HTTPError) -> RuntimeError:
    try:
        body = exc.read(512).decode("utf-8", errors="replace").strip()
    except OSError:
        body = ""
    detail = body or str(exc.reason)
    return RuntimeError(f"{method} {path} returned HTTP {exc.code}: {detail}")


# 调用 runtime HTTP API 并返回 JSON 值；重启后的只读查询允许短暂连接或服务错误重试
def _request_value(
    api_port: int,
    token: str,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
) -> Any:
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
    deadline = time.monotonic() + _READ_RETRY_TIMEOUT_S
    while True:
        try:
            with urllib.request.urlopen(request, timeout=5.0) as response:
                payload = json.loads(response.read())
            break
        except urllib.error.HTTPError as exc:
            retryable = method == "GET" and exc.code in _RETRYABLE_READ_STATUSES
            if not retryable or time.monotonic() >= deadline:
                raise _http_error(method, path, exc) from exc
            time.sleep(0.05)
        except (ConnectionError, OSError, urllib.error.URLError):
            if method != "GET" or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    return payload


# 调用 runtime HTTP API 并要求响应为 JSON 对象
def _request_json(
    api_port: int,
    token: str,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
) -> dict[str, Any]:
    payload = _request_value(api_port, token, method, path, body)
    if not isinstance(payload, dict):
        raise RuntimeError(f"API returned non-object payload for {path}")
    return payload


# 调用 runtime HTTP API 并要求响应为对象列表
def _request_list(
    api_port: int,
    token: str,
    method: str,
    path: str,
) -> list[dict[str, Any]]:
    payload = _request_value(api_port, token, method, path)
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise RuntimeError(f"API returned non-object-list payload for {path}")
    return payload


# 创建 turn；若 POST 响应超时则用隔离 thread 的新增 durable turn 安全恢复响应
def _create_turn_resilient(
    api_port: int,
    token: str,
    thread_id: str,
    content: str,
    known_turn_ids: set[str],
) -> dict[str, Any]:
    try:
        return _request_json(
            api_port,
            token,
            "POST",
            f"/v1/threads/{thread_id}/turns",
            {"content": content, "mode": "act"},
        )
    except (ConnectionError, OSError, urllib.error.URLError) as exc:
        deadline = time.monotonic() + _READ_RETRY_TIMEOUT_S
        while True:
            turns = _request_list(
                api_port,
                token,
                "GET",
                f"/v1/threads/{thread_id}/turns",
            )
            created = [turn for turn in turns if str(turn.get("id", "")) not in known_turn_ids]
            if len(created) == 1:
                return created[0]
            if len(created) > 1:
                raise RuntimeError(
                    "turn POST outcome is ambiguous: multiple new durable turns"
                ) from exc
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "turn POST outcome remained unknown after durable readback"
                ) from exc
            time.sleep(0.05)


# 等待阻塞模型确认收到本轮请求，确保强杀发生在活动 turn 内
def _wait_for_model_request(expected_count: int) -> None:
    deadline = time.monotonic() + _MODEL_REQUEST_TIMEOUT_S
    with _BlockingModelHandler.request_started:
        while _BlockingModelHandler.request_count < expected_count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"model request did not arrive within {_MODEL_REQUEST_TIMEOUT_S:g} seconds"
                )
            _BlockingModelHandler.request_started.wait(timeout=remaining)


# 等待运行时投影出未完成工具调用，确保强杀覆盖调用与终态结果之间的窗口
def _wait_for_tool_call(api_port: int, token: str, turn_id: str) -> None:
    deadline = time.monotonic() + _MODEL_REQUEST_TIMEOUT_S
    while time.monotonic() < deadline:
        items = _request_list(
            api_port,
            token,
            "GET",
            f"/v1/turns/{turn_id}/items",
        )
        if any(item.get("kind") == "tool_call" for item in items):
            return
        time.sleep(0.05)
    raise RuntimeError(
        f"tool call did not become durable within {_MODEL_REQUEST_TIMEOUT_S:g} seconds"
    )


# 等待运行时 SQLite 出现指定事件并返回解析后的事件载荷
def _wait_for_runtime_event(
    home: Path,
    turn_id: str,
    event_type: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + _MODEL_REQUEST_TIMEOUT_S
    database = home / ".coderook" / "runtime.db"
    while time.monotonic() < deadline:
        if database.is_file():
            try:
                with sqlite3.connect(database, timeout=1.0) as connection:
                    row = connection.execute(
                        "SELECT payload_json FROM runtime_events "
                        "WHERE turn_id = ? AND type = ? ORDER BY seq DESC LIMIT 1",
                        (turn_id, event_type),
                    ).fetchone()
                if row is not None:
                    payload = json.loads(str(row[0]))
                    return payload if isinstance(payload, dict) else {}
            except (OSError, sqlite3.Error, json.JSONDecodeError):
                pass
        time.sleep(0.05)
    raise RuntimeError(f"runtime event did not become durable: {event_type}")


# 等待工具结果写入运行时投影，确保强杀位于结果与 Turn 终态之间
def _wait_for_tool_result(api_port: int, token: str, turn_id: str) -> None:
    deadline = time.monotonic() + _MODEL_REQUEST_TIMEOUT_S
    while time.monotonic() < deadline:
        items = _request_list(api_port, token, "GET", f"/v1/turns/{turn_id}/items")
        if any(item.get("kind") == "tool_result" for item in items):
            return
        time.sleep(0.05)
    raise RuntimeError("tool result did not become durable")


# 等待长 Shell 写出子进程 PID，证明故障确实注入在受管进程树运行期间
def _wait_for_shell_pid(pid_file: Path) -> int:
    deadline = time.monotonic() + _MODEL_REQUEST_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            value = int(pid_file.read_text(encoding="utf-8").strip())
            if value > 0:
                return value
        except (OSError, UnicodeError, ValueError):
            pass
        time.sleep(0.05)
    raise RuntimeError("managed shell child did not start")


# 使用零信号探测 PID 是否仍存在，权限拒绝视为进程仍存活
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if getattr(exc, "winerror", None) == 87:
            return False
        raise
    return True


# 返回工具调用中没有对应终态结果的稳定标识
def _unmatched_tool_call_ids(items: list[dict[str, Any]]) -> list[str]:
    calls = {
        str(item.get("tool_call_id"))
        for item in items
        if item.get("kind") == "tool_call" and item.get("tool_call_id")
    }
    results = {
        str(item.get("tool_call_id"))
        for item in items
        if item.get("kind") == "tool_result" and item.get("tool_call_id")
    }
    return sorted(calls - results)


# 返回报告使用的完整 commit，优先采用 Actions 注入值并拒绝缩写
def _git_commit() -> str:
    candidate = os.environ.get("GITHUB_SHA", "").strip()
    if len(candidate) >= 40 and all(char in "0123456789abcdefABCDEF" for char in candidate):
        return candidate.lower()
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parent.parent,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    resolved = result.stdout.strip()
    return resolved if result.returncode == 0 and len(resolved) >= 40 else "unknown"


# 判断恢复率、完成数、基础设施与孤儿调用是否同时满足门禁
def _gate_passed(
    *,
    iterations: int,
    completed_iterations: int,
    recovery_rate: float,
    min_rate: float,
    orphaned_tool_calls: int,
    duplicate_modifications: int = 0,
    ledger_errors: int = 0,
    orphaned_processes: int = 0,
    infrastructure_error: str | None,
) -> bool:
    return (
        infrastructure_error is None
        and completed_iterations == iterations
        and recovery_rate >= min_rate
        and orphaned_tool_calls == 0
        and duplicate_modifications == 0
        and ledger_errors == 0
        and orphaned_processes == 0
    )


# 强制终止 daemon 并等待 OS 回收，模拟电源中断或进程强杀
def _hard_kill(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5.0)


# 执行真实 daemon 活动 turn 强杀/重启循环并输出逐轮 receipt 证据
def run_matrix(
    iterations: int,
    output: Path,
    *,
    min_rate: float = 0.95,
) -> dict[str, Any]:
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
    failure_context: dict[str, object] | None = None
    current_iteration: int | None = None
    current_phase: str | None = None
    current_stage = "initialize"
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
            token = (home / ".coderook" / "api-token").read_text(encoding="utf-8").strip()
            shell_pid_file = home / "managed-shell-child.pid"
            child_code = (
                "import os,time,pathlib;"
                f"pathlib.Path({str(shell_pid_file)!r}).write_text(str(os.getpid()),encoding='utf-8');"
                "time.sleep(60)"
            )
            parent_code = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
                "time.sleep(60)"
            )
            _BlockingModelHandler.shell_command = subprocess.list2cmdline(
                [sys.executable, "-c", parent_code]
            )
            phases = (
                "request_snapshot_in_flight",
                "tool_call_persisted",
                "shell_process_running",
                "permission_waiting",
                "tool_result_persisted",
            )
            prompts = {
                "request_snapshot_in_flight": "Inspect and explain the current repository.",
                "tool_call_persisted": "Run a shell command to inspect the environment.",
                "shell_process_running": "Run the requested long shell process.",
                "permission_waiting": "Run a shell command after requesting approval.",
                "tool_result_persisted": (
                    "Implement a file change by writing recovery-marker.txt with the "
                    "requested content."
                ),
            }
            for index in range(iterations):
                current_iteration = index + 1
                phase = phases[index % len(phases)]
                current_phase = phase
                with _BlockingModelHandler.request_started:
                    _BlockingModelHandler.request_phase = phase
                    _BlockingModelHandler.phase_request_count = 0
                    expected_request_count = _BlockingModelHandler.request_count + 1
                if shell_pid_file.exists():
                    shell_pid_file.unlink()
                current_stage = "create_thread"
                thread = _request_json(
                    api_port,
                    token,
                    "POST",
                    "/v1/threads",
                    {"title": f"crash matrix {index + 1}", "mode": "chat"},
                )
                thread_id = str(thread["id"])
                known_turn_ids: set[str] = set()
                current_stage = "create_turn"
                turn = _create_turn_resilient(
                    api_port,
                    token,
                    thread_id,
                    prompts[phase],
                    known_turn_ids,
                )
                turn_id = str(turn["id"])
                known_turn_ids.add(turn_id)
                current_stage = "wait_for_model_request"
                _wait_for_model_request(expected_request_count)
                shell_child_pid: int | None = None
                if phase in {
                    "tool_call_persisted",
                    "shell_process_running",
                    "permission_waiting",
                    "tool_result_persisted",
                }:
                    current_stage = "wait_for_tool_call"
                    _wait_for_tool_call(api_port, token, turn_id)
                if phase in {
                    "shell_process_running",
                    "permission_waiting",
                    "tool_result_persisted",
                }:
                    current_stage = "wait_for_permission"
                    permission = _wait_for_runtime_event(
                        home,
                        turn_id,
                        "permission.requested",
                    )
                    tool_use_id = str(permission.get("tool_use_id", ""))
                    if not tool_use_id:
                        raise RuntimeError("permission event omitted tool_use_id")
                    if phase != "permission_waiting":
                        current_stage = "approve_permission"
                        response = _request_json(
                            api_port,
                            token,
                            "POST",
                            f"/v1/permissions/{tool_use_id}",
                            {"decision": "allow_once"},
                        )
                        if not response.get("accepted"):
                            raise RuntimeError("permission response was not accepted")
                if phase == "shell_process_running":
                    current_stage = "wait_for_shell_process"
                    shell_child_pid = _wait_for_shell_pid(shell_pid_file)
                if phase == "tool_result_persisted":
                    current_stage = "wait_for_tool_result"
                    _wait_for_tool_result(api_port, token, turn_id)
                    _wait_for_model_request(expected_request_count + 1)
                current_stage = "hard_kill"
                recovery_started = time.monotonic()
                _hard_kill(process)
                current_stage = "restart_daemon"
                process = _start_daemon(env, home, api_port)
                current_stage = "read_turn"
                recovered = _request_json(
                    api_port,
                    token,
                    "GET",
                    f"/v1/turns/{turn_id}",
                )
                recovery_ms = int((time.monotonic() - recovery_started) * 1_000)
                current_stage = "read_receipt"
                receipt = _request_json(
                    api_port,
                    token,
                    "GET",
                    f"/v1/turns/{turn_id}/receipt",
                )
                current_stage = "read_items"
                items = _request_list(
                    api_port,
                    token,
                    "GET",
                    f"/v1/turns/{turn_id}/items",
                )
                unmatched_tool_calls = _unmatched_tool_call_ids(items)
                from code_rook.core.session.store import SessionStore

                ledger_issues = SessionStore(
                    home / ".coderook" / "sessions",
                    initialize=False,
                ).verify_ledger(thread_id)
                orphaned_process = False
                if shell_child_pid is not None:
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline and _pid_alive(shell_child_pid):
                        time.sleep(0.05)
                    orphaned_process = _pid_alive(shell_child_pid)
                marker_writes = 0
                marker_path = home / "recovery-marker.txt"
                if phase == "tool_result_persisted" and marker_path.is_file():
                    marker_writes = marker_path.read_text(encoding="utf-8").count("written-once")
                status = str(recovered.get("status", ""))
                error = dict(recovered.get("error") or {})
                expected_tool_calls = 0 if phase == "request_snapshot_in_flight" else 1
                passed = (
                    status == "interrupted"
                    and error.get("reason") == "daemon_restarted"
                    and receipt.get("status") == "interrupted"
                    and receipt.get("finished_at") is not None
                    and receipt.get("tool_call_count") == expected_tool_calls
                    and not unmatched_tool_calls
                    and not ledger_issues
                    and not orphaned_process
                    and (phase != "tool_result_persisted" or marker_writes == 1)
                    and (
                        phase != "permission_waiting"
                        or receipt.get("approvals", {}).get("requested") == 1
                    )
                )
                results.append(
                    {
                        "iteration": index + 1,
                        "phase": phase,
                        "turn_id": turn_id,
                        "status": status,
                        "reason": error.get("reason"),
                        "receipt_status": receipt.get("status"),
                        "tool_call_count": receipt.get("tool_call_count"),
                        "orphaned_tool_call_ids": unmatched_tool_calls,
                        "ledger_issues": ledger_issues,
                        "orphaned_high_risk_process": orphaned_process,
                        "marker_write_count": marker_writes,
                        "recovery_ms": recovery_ms,
                        "passed": passed,
                    }
                )
                current_stage = "iteration_complete"
            current_stage = "terminate_daemon"
            process.terminate()
            process.wait(timeout=5.0)
            process = None
    except Exception as exc:
        infrastructure_error = f"{type(exc).__name__}: {exc}"
        failure_context = {
            "iteration": current_iteration,
            "phase": current_phase,
            "stage": current_stage,
        }
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
        orphaned_tool_calls = sum(len(item.get("orphaned_tool_call_ids", [])) for item in results)
        duplicate_modifications = sum(
            max(0, int(item.get("marker_write_count", 0)) - 1) for item in results
        )
        ledger_errors = sum(len(item.get("ledger_issues", [])) for item in results)
        orphaned_processes = sum(bool(item.get("orphaned_high_risk_process")) for item in results)
        recovery_samples = [int(item["recovery_ms"]) for item in results]
        recovery_rate = passed / iterations if iterations else 0.0
        completed_iterations = len(results)
        report = {
            "schema_version": 4,
            "generated_at": datetime.now(UTC).isoformat(),
            "commit": _git_commit(),
            "platform": platform.system(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
            "iterations": iterations,
            "min_rate": min_rate,
            "model_request_timeout_s": _MODEL_REQUEST_TIMEOUT_S,
            "completed_iterations": completed_iterations,
            "passed": passed,
            "recovery_rate": recovery_rate,
            "orphaned_tool_calls": orphaned_tool_calls,
            "duplicate_modifications": duplicate_modifications,
            "ledger_errors": ledger_errors,
            "orphaned_high_risk_processes": orphaned_processes,
            "recovery_ms_p50": (statistics.median(recovery_samples) if recovery_samples else None),
            "recovery_ms_p95": (
                sorted(recovery_samples)[max(0, int(len(recovery_samples) * 0.95) - 1)]
                if recovery_samples
                else None
            ),
            "infrastructure_error": infrastructure_error,
            "failure_context": failure_context,
            "coverage": [
                "user_message_durable",
                "request_snapshot_in_flight",
                "tool_call_persisted_before_execution",
                "managed_shell_process_tree",
                "permission_waiting",
                "tool_result_persisted_before_turn_finish",
                "client_disconnect",
                "daemon_hard_kill",
                "restart_reconcile",
                "orphan_tool_call_repair",
                "receipt_rebuild",
            ],
            "gate_passed": _gate_passed(
                iterations=iterations,
                completed_iterations=completed_iterations,
                recovery_rate=recovery_rate,
                min_rate=min_rate,
                orphaned_tool_calls=orphaned_tool_calls,
                duplicate_modifications=duplicate_modifications,
                ledger_errors=ledger_errors,
                orphaned_processes=orphaned_processes,
                infrastructure_error=infrastructure_error,
            ),
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
        report = run_matrix(args.iterations, args.output, min_rate=args.min_rate)
    except (OSError, RuntimeError, urllib.error.URLError) as exc:
        print(f"crash recovery matrix failed: {exc}", file=sys.stderr)
        return 2
    rate = float(report["recovery_rate"])
    print(
        f"crash recovery: {report['passed']}/{report['iterations']} "
        f"({rate:.1%}); evidence={args.output}"
    )
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
