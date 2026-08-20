#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.smoke_installed_runtime import (  # noqa: E402
    _wait_until_listening,
    first_run_environment,
)

_DEFAULT_BASELINE_REF = "8cb9e1e421f16a246908c4d1b69840036a35237d"
_VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?:[.\-][0-9A-Za-z.\-]+)?")


# 解析升级 preflight 的基线、tag 严格度与证据输出参数
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build two CodeRook commits, preserve installed state across upgrade, "
            "then restore the backup and roll back."
        )
    )
    parser.add_argument("--baseline-ref", default=_DEFAULT_BASELINE_REF)
    parser.add_argument("--require-baseline-tag", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument(
        "--evidence",
        type=Path,
        default=_ROOT / "artifacts" / "upgrade-preflight.json",
    )
    return parser.parse_args()


# 运行外部命令并统一捕获 UTF-8 输出供失败诊断
def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 180.0,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        rendered = " ".join(command)
        raise RuntimeError(
            f"command failed ({exc.returncode}): {rendered}\n"
            f"stdout:\n{exc.stdout or ''}\nstderr:\n{exc.stderr or ''}"
        ) from exc


# 将 Git ref 解析为完整 commit，拒绝无法绑定身份的输入
def _resolve_commit(ref: str, *, root: Path = _ROOT) -> str:
    result = _run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=root)
    commit = result.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise RuntimeError(f"baseline ref did not resolve to a full commit: {ref}")
    return commit


# 返回精确指向 commit 的 tag，未发布候选保持空列表而不伪造版本身份
def _exact_tags(commit: str, *, root: Path = _ROOT) -> list[str]:
    result = _run(["git", "tag", "--points-at", commit], cwd=root)
    return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())


# 验证基线严格早于候选且位于同一祖先链
def _validate_commit_order(
    baseline_commit: str,
    candidate_commit: str,
    *,
    root: Path = _ROOT,
) -> None:
    if baseline_commit == candidate_commit:
        raise RuntimeError("baseline and candidate commits must differ")
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", baseline_commit, candidate_commit],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError("baseline commit is not an ancestor of the candidate")


# 检查候选工作树洁净，避免把未提交代码写成可复现升级证据
def _candidate_dirty(*, root: Path = _ROOT) -> bool:
    result = _run(["git", "status", "--porcelain"], cwd=root)
    return bool(result.stdout.strip())


# 安全解包指定 commit 的 Git archive 到隔离构建目录
def _export_commit(commit: str, destination: Path, *, root: Path = _ROOT) -> None:
    archive = subprocess.run(
        ["git", "archive", "--format=tar", commit],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        stream.extractall(destination, filter="data")


# 使用 uv 从指定源码目录构建唯一 wheel
def _build_wheel(source: Path, output: Path) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required for upgrade preflight")
    output.mkdir(parents=True, exist_ok=True)
    _run(
        [uv, "build", "--wheel", "--out-dir", str(output), str(source)],
        cwd=_ROOT,
        timeout=300,
    )
    wheels = sorted(output.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one wheel in {output}, found {len(wheels)}")
    return wheels[0]


# 返回跨平台虚拟环境 Python 路径
def _venv_python(environment: Path) -> Path:
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return environment / relative


# 创建不继承开发环境依赖的干净安装环境
def _create_environment(environment: Path) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required for upgrade preflight")
    _run(
        [
            uv,
            "venv",
            "--python",
            sys.executable,
            str(environment),
        ],
        cwd=_ROOT,
    )
    python = _venv_python(environment)
    if not python.is_file():
        raise RuntimeError(f"virtual environment Python is missing: {python}")
    return python


# 按 wheel 元数据强制安装目标及其依赖，真实覆盖依赖升级与降级
def _install_wheel(python: Path, wheel: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required for upgrade preflight")
    _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--reinstall",
            str(wheel),
        ],
        cwd=_ROOT,
        timeout=300,
    )


# 读取已安装 CLI 版本并规范化为数字版本字符串
def _installed_version(python: Path, *, workspace: Path, env: dict[str, str]) -> str:
    result = _run(
        [
            str(python),
            "-c",
            "from code_rook.cli.main import main; main()",
            "--version",
        ],
        cwd=workspace,
        env=env,
    )
    match = _VERSION_RE.search(result.stdout)
    if match is None:
        raise RuntimeError(f"installed CLI returned an invalid version: {result.stdout!r}")
    return match.group(0)


# 把版本转换为可比较的三段数字并拒绝非稳定格式
def _version_triplet(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        raise RuntimeError(f"upgrade preflight requires a stable x.y.z version: {version}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


# 计算文件 SHA-256 以绑定实际安装 artifact
def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# 对备份目录执行路径和内容排序哈希，证明回滚恢复的是同一快照
def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


# 向隔离 daemon 发送 JSON 请求并返回对象或对象列表
def _request_json(
    base_url: str,
    token: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"runtime API {method} {path} returned {exc.code}: {detail}") from exc


# 经已安装版本自身的 IPC 客户端请求有序关闭
def _request_shutdown(
    python: Path,
    *,
    workspace: Path,
    env: dict[str, str],
) -> None:
    source = """
import asyncio
from code_rook.core.config import get_config
from code_rook.core.transport.socket_client import SocketClient

async def stop():
    client = SocketClient.from_config(get_config())
    await client.connect()
    events = asyncio.create_task(client.run_event_loop())
    try:
        await client.send_command("core.shutdown", {"reason": "upgrade preflight"})
    finally:
        events.cancel()
        await asyncio.gather(events, return_exceptions=True)
        await client.close()

asyncio.run(stop())
"""
    _run(
        [str(python), "-c", source],
        cwd=workspace,
        env=env,
        timeout=10,
    )


@contextmanager
# 启动指定已安装版本的 daemon 并在退出时保证进程回收
def _running_daemon(
    python: Path,
    *,
    home: Path,
    workspace: Path,
) -> Iterator[tuple[str, str]]:
    env = first_run_environment(home)
    env["CODEROOK_API_TOKEN"] = env["CODEROOK_IPC_TOKEN"]
    process = subprocess.Popen(
        [str(python), "-c", "from code_rook.core.app import run; run()"],
        cwd=workspace,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        asyncio.run(_wait_until_listening(int(env["CODEROOK_PORT"]), process))
        yield (
            f"http://127.0.0.1:{env['CODEROOK_API_PORT']}",
            env["CODEROOK_API_TOKEN"],
        )
    finally:
        if process.poll() is None:
            try:
                _request_shutdown(python, workspace=workspace, env=env)
            except RuntimeError:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


# 在当前 daemon 创建持久 thread 并返回身份
def _create_thread(base_url: str, token: str, title: str) -> str:
    payload = _request_json(
        base_url,
        token,
        "/v1/threads",
        method="POST",
        body={"title": title, "mode": "chat"},
    )
    if not isinstance(payload, dict) or not payload.get("id"):
        raise RuntimeError("runtime API did not return a thread id")
    return str(payload["id"])


# 查询 thread 列表并返回稳定 id 集合
def _thread_ids(base_url: str, token: str) -> set[str]:
    payload = _request_json(base_url, token, "/v1/threads")
    if not isinstance(payload, list):
        raise RuntimeError("runtime API thread list is not an array")
    return {
        str(item["id"])
        for item in payload
        if isinstance(item, dict) and item.get("id")
    }


# 读取 runtime SQLite user_version 作为迁移结果证据
def _runtime_schema(home: Path) -> int:
    database = home / ".coderook" / "runtime.db"
    if not database.is_file():
        raise RuntimeError("runtime database was not created")
    with closing(sqlite3.connect(database)) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


# 复制完整用户状态快照并返回内容哈希
def _backup_state(home: Path, backup: Path) -> str:
    state = home / ".coderook"
    if not state.is_dir():
        raise RuntimeError("baseline did not create .coderook state")
    shutil.copytree(state, backup)
    return _tree_sha256(backup)


# 删除候选状态并原子语义恢复升级前完整备份
def _restore_state(home: Path, backup: Path) -> None:
    state = home / ".coderook"
    if state.parent.resolve() != home.resolve():
        raise RuntimeError("refusing to restore state outside isolated home")
    if state.exists():
        deadline = time.monotonic() + 5.0
        while True:
            try:
                shutil.rmtree(state)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "timed out waiting for the runtime state lock to release"
                    ) from None
                time.sleep(0.1)
    shutil.copytree(backup, state)


# 执行 baseline 安装、候选升级和备份回滚三阶段验证
def run_preflight(
    *,
    baseline_ref: str,
    evidence: Path,
    require_baseline_tag: bool = False,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    candidate_commit = _resolve_commit("HEAD")
    baseline_commit = _resolve_commit(baseline_ref)
    _validate_commit_order(baseline_commit, candidate_commit)
    dirty = _candidate_dirty()
    if dirty and not allow_dirty:
        raise RuntimeError("candidate worktree is dirty; commit changes before evidence")
    baseline_tags = _exact_tags(baseline_commit)
    if require_baseline_tag and not baseline_tags:
        raise RuntimeError("baseline commit has no exact Git tag")

    with tempfile.TemporaryDirectory(
        prefix="coderook-upgrade-preflight-",
        ignore_cleanup_errors=True,
    ) as raw_temp:
        root = Path(raw_temp)
        source = root / "baseline-source"
        wheels = root / "wheels"
        _export_commit(baseline_commit, source)
        baseline_wheel = _build_wheel(source, wheels / "baseline")
        candidate_wheel = _build_wheel(_ROOT, wheels / "candidate")
        python = _create_environment(root / "venv")
        home = root / "home"
        workspace = root / "workspace"
        backup = root / "backup-state"
        home.mkdir()
        workspace.mkdir()

        _install_wheel(python, baseline_wheel)
        baseline_env = first_run_environment(home)
        baseline_version = _installed_version(
            python,
            workspace=workspace,
            env=baseline_env,
        )
        with _running_daemon(python, home=home, workspace=workspace) as (url, token):
            baseline_thread = _create_thread(url, token, "Upgrade baseline thread")
            baseline_ids = _thread_ids(url, token)
            if baseline_thread not in baseline_ids:
                raise RuntimeError("baseline thread was not persisted")
        baseline_schema = _runtime_schema(home)
        backup_digest = _backup_state(home, backup)

        _install_wheel(python, candidate_wheel)
        candidate_env = first_run_environment(home)
        candidate_version = _installed_version(
            python,
            workspace=workspace,
            env=candidate_env,
        )
        if _version_triplet(candidate_version) <= _version_triplet(baseline_version):
            raise RuntimeError(
                f"candidate version {candidate_version} must exceed {baseline_version}"
            )
        with _running_daemon(python, home=home, workspace=workspace) as (url, token):
            upgraded_ids = _thread_ids(url, token)
            if baseline_thread not in upgraded_ids:
                raise RuntimeError("upgrade lost the baseline thread")
            candidate_thread = _create_thread(url, token, "Upgrade candidate thread")
            if candidate_thread not in _thread_ids(url, token):
                raise RuntimeError("candidate could not persist a new thread")
        candidate_schema = _runtime_schema(home)

        _restore_state(home, backup)
        if _tree_sha256(home / ".coderook") != backup_digest:
            raise RuntimeError("restored state does not match the backup digest")
        _install_wheel(python, baseline_wheel)
        rollback_env = first_run_environment(home)
        rollback_version = _installed_version(
            python,
            workspace=workspace,
            env=rollback_env,
        )
        with _running_daemon(python, home=home, workspace=workspace) as (url, token):
            rollback_ids = _thread_ids(url, token)
            if baseline_thread not in rollback_ids:
                raise RuntimeError("rollback did not restore the baseline thread")
            if candidate_thread in rollback_ids:
                raise RuntimeError("rollback backup unexpectedly contains candidate state")
        rollback_schema = _runtime_schema(home)

        report: dict[str, Any] = {
            "schema_version": 1,
            "status": "passed",
            "platform": sys.platform,
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "baseline": {
                "requested_ref": baseline_ref,
                "commit": baseline_commit,
                "exact_tags": baseline_tags,
                "tag_required": require_baseline_tag,
                "version": baseline_version,
                "wheel": baseline_wheel.name,
                "wheel_sha256": _file_sha256(baseline_wheel),
            },
            "candidate": {
                "commit": candidate_commit,
                "dirty": dirty,
                "version": candidate_version,
                "wheel": candidate_wheel.name,
                "wheel_sha256": _file_sha256(candidate_wheel),
            },
            "backup_sha256": backup_digest,
            "phases": {
                "baseline": {
                    "thread_id": baseline_thread,
                    "thread_count": len(baseline_ids),
                    "runtime_schema": baseline_schema,
                },
                "upgrade": {
                    "retained_thread_id": baseline_thread,
                    "created_thread_id": candidate_thread,
                    "thread_count": len(upgraded_ids) + 1,
                    "runtime_schema": candidate_schema,
                },
                "rollback": {
                    "version": rollback_version,
                    "restored_thread_id": baseline_thread,
                    "thread_count": len(rollback_ids),
                    "runtime_schema": rollback_schema,
                    "backup_hash_matches": True,
                },
            },
        }
    evidence = evidence.resolve()
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return report


# 运行升级 preflight 并打印机器可读证据位置
def main() -> int:
    args = _parse_args()
    try:
        report = run_preflight(
            baseline_ref=args.baseline_ref,
            evidence=args.evidence,
            require_baseline_tag=args.require_baseline_tag,
            allow_dirty=args.allow_dirty,
        )
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"upgrade preflight failed: {exc}") from exc
    print(
        "upgrade preflight passed: "
        f"{report['baseline']['version']} -> {report['candidate']['version']} -> "
        f"{report['phases']['rollback']['version']} evidence={args.evidence.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
