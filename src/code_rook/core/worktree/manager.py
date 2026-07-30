from __future__ import annotations

import asyncio
import re
from pathlib import Path

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


class WorktreeError(RuntimeError):
    pass


class WorktreeManager:
    # 初始化受项目根目录约束的 worktree 管理器
    def __init__(self, project_root: Path) -> None:
        self._root = project_root.resolve()
        self._dir = self._root / ".coderook" / "worktrees"

    # 校验名称并返回固定 worktree 路径，拒绝路径穿越
    def path_for(self, name: str) -> Path:
        if not _NAME_RE.fullmatch(name):
            raise WorktreeError("invalid worktree name")
        return self._dir / name

    # 创建独立分支和 worktree，并返回绝对路径
    async def create(self, name: str, base_ref: str = "HEAD") -> Path:
        path = self.path_for(name)
        if path.exists():
            raise WorktreeError(f"worktree already exists: {name}")
        self._dir.mkdir(parents=True, exist_ok=True)
        branch = f"coderook/{name}"
        await self._git("worktree", "add", str(path), "-b", branch, base_ref)
        return path

    # 列出由 CodeRook 固定目录管理的 worktree 名称和路径
    async def list(self) -> list[dict[str, str]]:
        if not self._dir.exists():
            return []
        output = await self._git("worktree", "list", "--porcelain")
        managed: list[dict[str, str]] = []
        current_path = ""
        branch = ""
        for line in (output + "\n").splitlines():
            if line.startswith("worktree "):
                current_path = line[9:]
            elif line.startswith("branch "):
                branch = line[7:]
            elif not line and current_path:
                candidate = Path(current_path).resolve()
                try:
                    name = candidate.relative_to(self._dir.resolve()).as_posix()
                except ValueError:
                    current_path, branch = "", ""
                    continue
                managed.append({"name": name, "path": str(candidate), "branch": branch})
                current_path, branch = "", ""
        return managed

    # 删除受管 worktree；默认拒绝丢弃未提交修改
    async def remove(self, name: str, discard_changes: bool = False) -> None:
        path = self.path_for(name)
        if not path.exists():
            raise WorktreeError(f"worktree not found: {name}")
        status = await self._git("-C", str(path), "status", "--porcelain")
        if status.strip() and not discard_changes:
            raise WorktreeError("worktree has uncommitted changes")
        args = ["worktree", "remove", str(path)]
        if discard_changes:
            args.append("--force")
        await self._git(*args)

    # 在项目仓库中运行 git 子命令，失败时转换为领域错误
    async def _git(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self._root),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        output = stdout.decode(errors="replace")
        if process.returncode != 0:
            message = stderr.decode(errors="replace").strip() or output.strip()
            raise WorktreeError(message or f"git exited with {process.returncode}")
        return output
