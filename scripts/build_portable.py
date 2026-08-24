#!/usr/bin/env python3
from __future__ import annotations

import argparse
import platform
import shutil
import stat
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

_HOST_TARGETS = {
    ("windows", "amd64"): "windows-x86_64",
    ("windows", "x86_64"): "windows-x86_64",
    ("linux", "amd64"): "linux-x86_64",
    ("linux", "x86_64"): "linux-x86_64",
    ("linux", "aarch64"): "linux-arm64",
    ("linux", "arm64"): "linux-arm64",
    ("darwin", "amd64"): "macos-x86_64",
    ("darwin", "x86_64"): "macos-x86_64",
    ("darwin", "aarch64"): "macos-arm64",
    ("darwin", "arm64"): "macos-arm64",
}


# 解析跨平台自包含目录包构建参数
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a self-contained CodeRook package.")
    parser.add_argument(
        "--target",
        required=True,
        choices=(
            "windows-x86_64",
            "linux-x86_64",
            "linux-arm64",
            "macos-x86_64",
            "macos-arm64",
        ),
    )
    parser.add_argument("--output-root", type=Path, default=_ROOT / "dist")
    return parser.parse_args()


# 运行构建子命令并在失败时保留清晰的命令边界
def _run(*argv: str, cwd: Path = _ROOT) -> str:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


# 将当前宿主系统和 CPU 规范化为唯一便携包 target
def _current_host_target() -> str:
    system = platform.system().strip().lower()
    machine = platform.machine().strip().lower()
    target = _HOST_TARGETS.get((system, machine))
    if target is None:
        raise RuntimeError(
            f"unsupported portable build host: system={system or 'unknown'} "
            f"machine={machine or 'unknown'}"
        )
    return target


# 拒绝用本机解释器生成伪装成其他操作系统或架构的发行包
def _require_native_target(target: str) -> None:
    host_target = _current_host_target()
    if target != host_target:
        raise RuntimeError(
            f"portable target {target!r} does not match native build host {host_target!r}; "
            "cross-platform portable builds are not supported"
        )


# 查找 uv 管理的 CPython 3.12 根目录和实际解释器
def _managed_python(target: str) -> tuple[Path, Path]:
    executable = Path(
        _run("uv", "python", "find", "--managed-python", "3.12")
    ).resolve()
    if not executable.is_file():
        raise RuntimeError("uv-managed Python 3.12 executable was not found")
    runtime_root = executable.parent if target.startswith("windows-") else executable.parent.parent
    return runtime_root, executable


# 在便携目录中写入不依赖绝对安装路径的唯一 coderook 启动器
def _write_launcher(package_root: Path, target: str) -> Path:
    if target.startswith("windows-"):
        launcher = package_root / "coderook.cmd"
        launcher.write_text(
            "@echo off\r\n"
            "set \"CODEROOK_PORTABLE_ROOT=%~dp0\"\r\n"
            "set \"PATH=%CODEROOK_PORTABLE_ROOT%runtime;"
            "%CODEROOK_PORTABLE_ROOT%runtime\\Scripts;%PATH%\"\r\n"
            '"%CODEROOK_PORTABLE_ROOT%runtime\\python.exe" -c '
            '"from code_rook.cli.main import main; raise SystemExit(main())" %*\r\n',
            encoding="utf-8",
            newline="",
        )
        return launcher
    launcher = package_root / "coderook"
    launcher.write_text(
        "#!/bin/sh\n"
        'CODEROOK_PORTABLE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'PATH="$CODEROOK_PORTABLE_ROOT/runtime/bin:$PATH"\n'
        "export PATH\n"
        'exec "$CODEROOK_PORTABLE_ROOT/runtime/bin/python3.12" -c '
        "'from code_rook.cli.main import main; raise SystemExit(main())' \"$@\"\n",
        encoding="utf-8",
        newline="\n",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return launcher


# 复制受控 Python runtime、安装 wheel 并生成可移动压缩包
def build_portable(target: str, output_root: Path) -> Path:
    _require_native_target(target)
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    package_root = output_root / f"coderook-{target}"
    if package_root.exists():
        shutil.rmtree(package_root)
    _run("uv", "build", "--wheel", "--out-dir", str(output_root))
    wheels = sorted(output_root.glob("coderook-*.whl"), key=lambda path: path.stat().st_mtime)
    if not wheels:
        raise RuntimeError("CodeRook wheel was not produced")
    runtime_source, _source_python = _managed_python(target)
    runtime_target = package_root / "runtime"
    shutil.copytree(runtime_source, runtime_target, symlinks=False)
    portable_python = (
        runtime_target / "python.exe"
        if target.startswith("windows-")
        else runtime_target / "bin" / "python3.12"
    )
    _run(
        "uv",
        "pip",
        "install",
        "--python",
        str(portable_python),
        "--break-system-packages",
        str(wheels[-1]),
    )
    _write_launcher(package_root, target)
    archive_format = "zip" if target.startswith("windows-") else "gztar"
    archive = shutil.make_archive(
        str(output_root / f"coderook-{target}"),
        archive_format,
        root_dir=output_root,
        base_dir=package_root.name,
    )
    return Path(archive)


# 执行便携包构建并打印唯一产物路径供 workflow 消费
def main() -> int:
    args = _parse_args()
    archive = build_portable(args.target, args.output_root)
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
