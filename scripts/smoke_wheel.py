#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

_CREDENTIAL_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
_CORE_START_TIMEOUT_S = 30.0


# 定位待验证目录中唯一的 wheel 文件
def _find_wheel(path: Path) -> Path:
    candidates = sorted(path.glob("coderook-*.whl")) if path.is_dir() else [path]
    if len(candidates) != 1 or not candidates[0].is_file():
        raise SystemExit(f"expected exactly one wheel, found: {candidates}")
    return candidates[0].resolve()


# 校验 wheel 成员路径后安全解压到临时目录
def _safe_extract(wheel: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if not target.is_relative_to(destination):
                raise SystemExit(f"unsafe wheel member: {member.filename}")
        archive.extractall(destination)


# 向操作系统申请一个临时可用的 loopback 端口
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# 在隔离环境中执行一段 Python 代码并捕获输出
def _run_python(
    code: str,
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


# 构造不继承开发机凭据与 CodeRook 状态的首次运行环境
def _first_run_environment(home: Path, site: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CODEROOK_")
        and not key.upper().endswith(_CREDENTIAL_SUFFIXES)
        and key not in {"PYTHONPATH"}
    }
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONPATH": str(site),
            "PYTHONUTF8": "1",
            "CODEROOK_HOST": "127.0.0.1",
            "CODEROOK_PORT": str(_free_port()),
            "CODEROOK_API_PORT": str(_free_port()),
            "CODEROOK_IPC_TOKEN": secrets.token_urlsafe(32),
            "CODEROOK_LOG_FILE": "",
            "CODEROOK_LOG_LEVEL": "WARNING",
            "CODEROOK_TRACE_ENABLED": "false",
        }
    )
    return env


# 验证全新安装以非阻塞未配置状态启动且不会伪造 provider、model 或凭据
def _assert_first_run_status(output: str) -> None:
    required = (
        "status:   unconfigured",
        "provider: (none)",
        "model:    (none)",
        "endpoint: (none)",
        "credential: missing",
        "validation: not_run",
    )
    if any(item not in output for item in required):
        raise RuntimeError(f"unexpected first-run config status: {output!r}")


# 等待 wheel 中启动的 Core 开始监听或提前失败
async def _wait_until_listening(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + _CORE_START_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(
                f"wheel Core exited with {process.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise TimeoutError(
        f"wheel Core did not listen within {_CORE_START_TIMEOUT_S:g} seconds "
        f"(pid={process.pid}, returncode={process.poll()})"
    )


# 在 Windows daemon 退出后的短暂句柄释放窗口内重试删除隔离目录
def _remove_tree_with_retry(path: Path) -> None:
    for attempt in range(20):
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.1)


# 从解压后的 wheel 验证资源、入口、鉴权 Core 和真实 ping
def smoke(wheel: Path) -> None:
    started_at = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix="coderook-wheel-smoke-",
        ignore_cleanup_errors=True,
    ) as raw_temp:
        root = Path(raw_temp)
        site = root / "site"
        site.mkdir()
        _safe_extract(wheel, site)

        required_resources = [
            site / "code_rook" / "py.typed",
            site / "code_rook" / "core" / "agents" / "builtin" / "executor.toml",
            site / "code_rook" / "core" / "skills" / "builtin" / "review.md",
            site / "code_rook" / "web" / "static" / "index.html",
            site / "code_rook" / "web" / "static" / "manifest.webmanifest",
        ]
        missing = [str(path.relative_to(site)) for path in required_resources if not path.is_file()]
        if missing:
            raise RuntimeError(f"wheel is missing package resources: {missing}")

        home = root / "home"
        home.mkdir()
        env = _first_run_environment(home, site)

        imported = _run_python(
            """
from pathlib import Path
import os
import code_rook
package = Path(code_rook.__file__).resolve()
site = Path(os.environ["PYTHONPATH"]).resolve()
assert package.is_relative_to(site), (package, site)
print(package)
""",
            env=env,
            cwd=root,
        )
        if "code_rook" not in imported.stdout:
            raise RuntimeError("wheel package import did not report its path")

        _run_python(
            "import sys; sys.argv=['coderook', '--version']; "
            "from code_rook.cli.main import main; main()",
            env=env,
            cwd=root,
        )
        _run_python(
            "import sys; sys.argv=['coderook-tui', '--help']; "
            "from code_rook.tui.__main__ import main; main()",
            env=env,
            cwd=root,
        )
        status = _run_python(
            "import sys; sys.argv=['coderook', 'config-status']; "
            "from code_rook.cli.main import main; main()",
            env=env,
            cwd=root,
        )
        _assert_first_run_status(status.stdout)

        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from code_rook.core.app import run; run()",
            ],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            asyncio.run(_wait_until_listening(int(env["CODEROOK_PORT"]), process))
            ping = _run_python(
                "import sys; sys.argv=['coderook', 'ping']; "
                "from code_rook.cli.main import main; main()",
                env=env,
                cwd=root,
            )
            if not ping.stdout.startswith("pong server="):
                raise RuntimeError(f"unexpected wheel ping output: {ping.stdout!r}")
            web_launch = _run_python(
                "import sys; sys.argv=['coderook', 'web', '--no-open']; "
                "from code_rook.cli.main import main; main()",
                env=env,
                cwd=root,
            )
            launch_url = web_launch.stdout.strip()
            if not launch_url.startswith("http://127.0.0.1:") or "#launch=" not in launch_url:
                raise RuntimeError(f"unexpected wheel Web launch URL: {launch_url!r}")
            web_shell = _run_python(
                "import os, urllib.request; "
                "url='http://127.0.0.1:' + os.environ['CODEROOK_API_PORT'] + '/'; "
                "print(urllib.request.urlopen(url, timeout=5).read().decode('utf-8'))",
                env=env,
                cwd=root,
            )
            if "CodeRook Web" not in web_shell.stdout:
                raise RuntimeError("wheel Web shell did not load from the running Core")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)

    _remove_tree_with_retry(Path(raw_temp))
    elapsed = time.monotonic() - started_at
    print(f"wheel first-run smoke passed: {wheel.name} elapsed={elapsed:.2f}s")


# 解析命令行参数并执行 wheel 烟测
def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test a built CodeRook wheel")
    parser.add_argument("wheel_or_dist", type=Path)
    args = parser.parse_args()
    smoke(_find_wheel(args.wheel_or_dist))


if __name__ == "__main__":
    main()
