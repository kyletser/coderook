from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from code_rook.core.lsp.diagnostics import DiagnosticsReport, parse_pyright_diagnostics
from code_rook.core.processes import ProcessSupervisor, terminate_process_tree
from code_rook.core.workspace import WorkspaceBoundary, WorkspaceBoundaryError

_DEFAULT_TIMEOUT_S = 5.0
_DEFAULT_MAX_OUTPUT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class _CommandOutput:
    returncode: int
    stdout: str
    truncated: bool


# 启动独立进程组并读取有界合并输出，超限时终止整棵进程树
async def _run_bounded_command(
    executable: str,
    args: list[str],
    cwd: Path,
    *,
    timeout_s: float,
    max_output_bytes: int,
    process_supervisor: ProcessSupervisor | None = None,
) -> _CommandOutput:
    platform_options: dict[str, object] = (
        {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    if process_supervisor is not None:
        process = await process_supervisor.start_exec(
            executable,
            *args,
            label="workspace-diagnostics",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    else:
        process = await asyncio.create_subprocess_exec(
            executable,
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **platform_options,  # type: ignore[arg-type]
        )
    reader = process.stdout
    assert reader is not None

    # 持续读取直到 EOF 或超过边界，避免 communicate 无界累积输出
    async def _collect() -> _CommandOutput:
        output = bytearray()
        while True:
            chunk = await reader.read(8192)
            if not chunk:
                break
            remaining = max_output_bytes + 1 - len(output)
            output.extend(chunk[:remaining])
            if len(output) > max_output_bytes:
                if process_supervisor is not None:
                    await process_supervisor.terminate(process)
                else:
                    await terminate_process_tree(process)
                return _CommandOutput(
                    returncode=process.returncode or -1,
                    stdout=bytes(output[:max_output_bytes]).decode(
                        "utf-8", errors="replace"
                    ),
                    truncated=True,
                )
        returncode = await process.wait()
        if process_supervisor is not None:
            process_supervisor.forget(process)
        return _CommandOutput(
            returncode=returncode,
            stdout=bytes(output).decode("utf-8", errors="replace"),
            truncated=False,
        )

    try:
        return await asyncio.wait_for(_collect(), timeout=timeout_s)
    except (TimeoutError, asyncio.CancelledError):
        if process_supervisor is not None:
            await process_supervisor.terminate(process)
        else:
            await terminate_process_tree(process)
        raise


class PythonDiagnosticsClient:
    # 自动探测 basedpyright/pyright，并保存超时与输出上限
    def __init__(
        self,
        boundary: WorkspaceBoundary,
        *,
        executable: str | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        process_supervisor: ProcessSupervisor | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        self._boundary = boundary
        self._executable = executable or shutil.which("basedpyright") or shutil.which(
            "pyright"
        )
        self._timeout_s = timeout_s
        self._max_output_bytes = max_output_bytes
        self._process_supervisor = process_supervisor

    # 返回实际探测到的诊断工具名称，缺失时为空
    @property
    def tool_name(self) -> str:
        return Path(self._executable).stem if self._executable else ""

    # 对修改过的 Python 文件运行有界诊断并把所有失败转换为结构化降级结果
    async def diagnose(self, paths: list[str]) -> DiagnosticsReport:
        if self._executable is None:
            return DiagnosticsReport(status="unavailable")
        relative_paths: list[str] = []
        for raw_path in dict.fromkeys(paths):
            if not raw_path.casefold().endswith(".py"):
                continue
            try:
                path = self._boundary.resolve(raw_path)
                relative_paths.append(path.relative_to(self._boundary.root).as_posix())
            except (WorkspaceBoundaryError, OSError, RuntimeError, ValueError):
                continue
        if not relative_paths:
            return DiagnosticsReport(status="ok", tool=self.tool_name)
        try:
            output = await _run_bounded_command(
                self._executable,
                ["--outputjson", *relative_paths],
                self._boundary.root,
                timeout_s=self._timeout_s,
                max_output_bytes=self._max_output_bytes,
                process_supervisor=self._process_supervisor,
            )
        except TimeoutError:
            return DiagnosticsReport(
                status="timeout",
                tool=self.tool_name,
                error=f"diagnostics exceeded {self._timeout_s:g}s timeout",
            )
        except (OSError, RuntimeError) as exc:
            return DiagnosticsReport(
                status="failed",
                tool=self.tool_name,
                error=str(exc)[:500],
            )
        if output.truncated:
            return DiagnosticsReport(
                status="truncated",
                tool=self.tool_name,
                truncated=True,
                error=(
                    "diagnostics output exceeded "
                    f"{self._max_output_bytes} bytes"
                ),
            )
        try:
            payload = json.loads(output.stdout)
        except json.JSONDecodeError as exc:
            return DiagnosticsReport(
                status="failed",
                tool=self.tool_name,
                error=f"invalid diagnostics JSON: {exc}",
            )
        if not isinstance(payload, dict):
            return DiagnosticsReport(
                status="failed",
                tool=self.tool_name,
                error="diagnostics JSON root must be an object",
            )
        diagnostics, truncated = parse_pyright_diagnostics(
            payload,
            self._boundary,
        )
        return DiagnosticsReport(
            status="ok",
            tool=self.tool_name,
            diagnostics=diagnostics,
            truncated=truncated,
        )
