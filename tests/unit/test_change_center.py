from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from code_rook.core import change_center as change_center_module
from code_rook.core.change_center import ChangeCenterError, ChangeCenterService
from code_rook.core.workspace import WorkspaceBoundary


# 初始化带本地身份和基线提交的测试仓库
def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "CodeRook Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "coderook@example.invalid"],
        check=True,
    )
    (path / "one.txt").write_text("one\n", encoding="utf-8")
    (path / "two.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-qm", "base"],
        check=True,
    )


# 初始化尚无提交和 index 的未出生分支测试仓库
def _init_unborn_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "CodeRook Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "coderook@example.invalid"],
        check=True,
    )


# 返回指定 diff scope 的 NUL 分隔文件名集合
def _diff_names(path: Path, *args: str) -> set[str]:
    output = subprocess.run(
        ["git", "-C", str(path), "diff", "--name-only", "-z", *args],
        check=True,
        capture_output=True,
    ).stdout
    return {
        item.decode("utf-8")
        for item in output.split(b"\0")
        if item
    }


# 功能：Change Center 只 stage 用户选择的当前变更并保留其他 worktree 修改
# 设计：同时修改两个文件，仅选择一个后核对 staged/unstaged 两套 Git 权威集合
async def test_stage_selected_files_only(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "one.txt").write_text("one changed\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two changed\n", encoding="utf-8")
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff()

    result = await service.stage(
        ["one.txt"],
        expected_digest=str(reviewed["state_digest"]),
    )

    assert _diff_names(tmp_path, "--cached") == {"one.txt"}
    assert _diff_names(tmp_path) == {"two.txt"}
    assert [item["path"] for item in result["files"]] == ["one.txt"]


# 功能：stage 拒绝越界路径和并非当前改动的文件
# 设计：分别传父目录路径与未改文件，证明边界校验和 changed-set 校验均 fail closed
async def test_stage_rejects_outside_and_unchanged_paths(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff()
    digest = str(reviewed["state_digest"])

    with pytest.raises(ChangeCenterError, match="workspace-relative|outside"):
        await service.stage(["../outside.txt"], expected_digest=digest)
    with pytest.raises(ChangeCenterError, match="not current workspace changes"):
        await service.stage(["one.txt"], expected_digest=digest)


# 功能：Change Center 在审查后工作区发生变化时拒绝继续 stage
# 设计：先固定状态摘要再写入同一文件，断言 TOCTOU 重验发生在任何 Git mutation 之前
async def test_stage_rejects_stale_review_digest(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff()
    (tmp_path / "one.txt").write_text("changed after review\n", encoding="utf-8")

    with pytest.raises(ChangeCenterError, match="changed after review"):
        await service.stage(
            ["one.txt"],
            expected_digest=str(reviewed["state_digest"]),
        )

    assert _diff_names(tmp_path, "--cached") == set()


# 功能：可见 diff 生成后仓库变成另一状态时不得返回绑定新摘要的旧画面
# 设计：在 GitDiffTool 已产出 A 后确定性写入 B，断言 Change Center 拒绝“展示 A、摘要 B”
async def test_diff_rejects_visible_a_with_digest_b_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "one.txt"
    target.write_text("reviewed A\n", encoding="utf-8")
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    original_invoke = change_center_module.GitDiffTool.invoke
    visible_diffs: list[str] = []

    # 在结构化可见结果返回后、Change Center 绑定摘要前注入并发修改
    async def mutate_after_visible(
        tool: object,
        params: dict[str, object],
    ) -> object:
        result = await original_invoke(tool, params)  # type: ignore[arg-type]
        payload = json.loads(result.content)
        visible_diffs.append(str(payload["diff"]))
        target.write_text("staged B\n", encoding="utf-8")
        return result

    monkeypatch.setattr(change_center_module.GitDiffTool, "invoke", mutate_after_visible)

    with pytest.raises(ChangeCenterError, match="changed while preparing visible review"):
        await service.diff()

    assert "+reviewed A" in visible_diffs[0]
    assert target.read_text(encoding="utf-8") == "staged B\n"


# 功能：stage 在审查 A 后临时 index 捕获 B 时拒绝发布真实 index
# 设计：只在 alternate-index git add 前写入 B，证明最终摘要复核阻断“审查 A、stage B”
async def test_stage_rejects_content_changed_during_temporary_add(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "one.txt"
    target.write_text("reviewed A\n", encoding="utf-8")
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff()
    original_run = service._run
    mutated = False

    # 在临时 index 真正读取工作区前注入另一份同路径内容
    async def mutate_before_temporary_add(
        *args: str,
        environment_overrides: dict[str, str] | None = None,
        allowed_returncodes: frozenset[int] = frozenset({0}),
    ) -> object:
        nonlocal mutated
        if args[:1] == ("add",) and environment_overrides and not mutated:
            target.write_text("staged B\n", encoding="utf-8")
            mutated = True
        return await original_run(
            *args,
            environment_overrides=environment_overrides,
            allowed_returncodes=allowed_returncodes,
        )

    monkeypatch.setattr(service, "_run", mutate_before_temporary_add)

    with pytest.raises(ChangeCenterError, match="changed after review"):
        await service.stage(
            ["one.txt"],
            expected_digest=str(reviewed["state_digest"]),
        )

    assert mutated is True
    assert _diff_names(tmp_path, "--cached") == set()
    assert target.read_text(encoding="utf-8") == "staged B\n"


# 功能：未跟踪文件在审查后被同长内容替换时摘要变化并阻断 stage
# 设计：保持路径、Git 状态和字节长度不变，隔离验证摘要确实绑定文件正文
async def test_stage_rejects_replaced_untracked_content(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    untracked = tmp_path / "new.txt"
    untracked.write_bytes(b"reviewed")
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff()

    untracked.write_bytes(b"replaced")

    assert await service.state_digest() != reviewed["state_digest"]
    with pytest.raises(ChangeCenterError, match="changed after review"):
        await service.stage(
            ["new.txt"],
            expected_digest=str(reviewed["state_digest"]),
        )
    assert _diff_names(tmp_path, "--cached") == set()


# 功能：完整进入可见 diff 的未跟踪文本可以按显式选择加入 index
# 设计：先核对 synthetic hunk 与 review_complete，再执行 stage 并检查新文件成为 staged
async def test_stage_accepts_fully_reviewed_untracked_text(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "new.txt").write_text("visible body\n", encoding="utf-8")
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff()
    file_info = next(
        item for item in reviewed["files"] if item["path"] == "new.txt"
    )

    assert file_info["review_complete"] is True
    assert "+visible body" in str(reviewed["diff"])
    await service.stage(
        ["new.txt"],
        expected_digest=str(reviewed["state_digest"]),
    )

    assert _diff_names(tmp_path, "--cached") == {"new.txt"}


# 功能：小型未跟踪二进制只有完整长度与摘要时才允许显式 stage
# 设计：使用 NUL 字节确保不生成文本 hunk，同时断言安全元数据和 index 结果一致
async def test_stage_accepts_reviewed_untracked_binary_metadata(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "asset.bin").write_bytes(b"\x00binary\xff")
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff()
    file_info = next(
        item for item in reviewed["files"] if item["path"] == "asset.bin"
    )

    assert file_info["review_status"] == "binary"
    assert file_info["review_complete"] is True
    assert len(str(file_info["content_sha256"])) == 64
    await service.stage(
        ["asset.bin"],
        expected_digest=str(reviewed["state_digest"]),
    )

    assert _diff_names(tmp_path, "--cached") == {"asset.bin"}


# 功能：未跟踪正文超过可见审查上限时 stage 必须在 Git mutation 前失败关闭
# 设计：创建略大于硬上限的文本并传回同一摘要，断言阻断原因和空 index
async def test_stage_rejects_unreviewable_oversized_untracked_file(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "large.txt").write_text("x" * 210_000, encoding="utf-8")
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff()
    file_info = next(
        item for item in reviewed["files"] if item["path"] == "large.txt"
    )

    assert file_info["review_complete"] is False
    assert file_info["review_status"] == "truncated"
    with pytest.raises(ChangeCenterError, match="complete visible review"):
        await service.stage(
            ["large.txt"],
            expected_digest=str(reviewed["state_digest"]),
        )

    assert _diff_names(tmp_path, "--cached") == set()


# 功能：外部预先 stage 的超大补丁仍不能绕过 Change Center 的 commit 审查门禁
# 设计：直接用 Git stage 大文件后只取得摘要，再断言 commit 拒绝且 HEAD 保持不变
async def test_commit_rejects_externally_staged_unreviewable_patch(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    head_before = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (tmp_path / "large.txt").write_text("y" * 210_000, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "large.txt"],
        check=True,
    )
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff("staged")

    with pytest.raises(ChangeCenterError, match="complete visible review"):
        await service.commit(
            "must not commit opaque patch",
            expected_digest=str(reviewed["state_digest"]),
        )

    head_after = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head_after == head_before


# 功能：大型未跟踪文件的尾部变化会被内容摘要检出
# 设计：使用超过两个哈希分块的文件并仅改最后字节，覆盖有界流式读取的尾块
async def test_digest_streams_large_untracked_file(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    untracked = tmp_path / "large.bin"
    untracked.write_bytes(b"a" * (2 * 1024 * 1024 + 17))
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))

    before = await service.state_digest()
    with untracked.open("r+b") as handle:
        handle.seek(-1, os.SEEK_END)
        handle.write(b"b")
    after = await service.state_digest()

    assert after != before


# 功能：未跟踪文件在分块哈希过程中被写入时失败关闭
# 设计：包装目标 fd 的首次 os.read 并在返回后改写文件，稳定命中读取前后元数据复核
async def test_digest_fails_closed_when_untracked_file_changes_during_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "changing.bin"
    target.write_bytes(b"a" * (2 * 1024 * 1024))
    target_stat = os.lstat(target)
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    original_read = os.read
    mutated = False

    # 仅在读取目标文件的描述符时注入并发写入
    def mutating_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = original_read(descriptor, size)
        descriptor_stat = os.fstat(descriptor)
        if not mutated and (
            descriptor_stat.st_dev == target_stat.st_dev
            and descriptor_stat.st_ino == target_stat.st_ino
        ):
            with target.open("r+b") as handle:
                handle.write(b"b")
                handle.flush()
                os.fsync(handle.fileno())
            mutated = True
        return chunk

    monkeypatch.setattr(os, "read", mutating_read)

    with pytest.raises(ChangeCenterError, match="changed while computing"):
        await service.state_digest()
    assert mutated is True


# 功能：未跟踪符号链接只绑定链接目标文本而不读取工作区外内容
# 设计：链接指向外部文件，分别修改外部正文和重建链接目标以区分两种语义
async def test_digest_does_not_follow_untracked_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_repo(workspace)
    outside_one = tmp_path / "outside-one.txt"
    outside_two = tmp_path / "outside-two.txt"
    outside_one.write_text("first secret", encoding="utf-8")
    outside_two.write_text("second secret", encoding="utf-8")
    link = workspace / "external-link.txt"
    try:
        link.symlink_to(outside_one)
    except OSError:
        pytest.skip("symbolic links are unavailable on this platform")
    service = ChangeCenterService(WorkspaceBoundary(workspace))

    before = await service.state_digest()
    outside_one.write_text("changed secret", encoding="utf-8")
    after_target_content_change = await service.state_digest()
    link.unlink()
    link.symlink_to(outside_two)
    after_link_change = await service.state_digest()

    assert after_target_content_change == before
    assert after_link_change != before


# 功能：未跟踪文件在哈希途中消失时摘要计算失败关闭
# 设计：将目标的第二次 lstat 稳定模拟为消失，避免依赖平台对已打开文件的删除语义
async def test_digest_fails_closed_when_untracked_file_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "vanishing.txt"
    target.write_text("content", encoding="utf-8")
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    original_lstat = os.lstat
    calls = 0

    # 在目标文件哈希后的复核点模拟文件已消失
    def disappearing_lstat(path: os.PathLike[str] | str) -> os.stat_result:
        nonlocal calls
        if Path(path) == target:
            calls += 1
            if calls == 2:
                raise FileNotFoundError(path)
        return original_lstat(path)

    monkeypatch.setattr(os, "lstat", disappearing_lstat)

    with pytest.raises(ChangeCenterError, match="changed while computing"):
        await service.state_digest()


# 功能：本地 commit 只提交 staged 内容且不会隐式执行仓库 Git hooks
# 设计：安装会落 marker 的 pre-commit，提交后检查 hash/文件并确认 marker 未创建
async def test_commit_is_local_and_skips_repository_hooks(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    marker = tmp_path / "hook-ran.txt"
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\nprintf unsafe > hook-ran.txt\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    (tmp_path / "one.txt").write_text("committed\n", encoding="utf-8")
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff()
    staged = await service.stage(
        ["one.txt"],
        expected_digest=str(reviewed["state_digest"]),
    )

    result = await service.commit(
        "local reviewed change",
        expected_digest=str(staged["state_digest"]),
    )

    assert len(result.commit) == 40
    assert result.files == ("one.txt",)
    assert result.hooks_skipped is True
    assert marker.exists() is False
    assert _diff_names(tmp_path, "--cached") == set()


# 功能：commit 在审查 staged A 后 index 被替换为 B 时不得移动 HEAD
# 设计：在最终 index lock 取得前确定性执行外部 git add，断言摘要复核拒绝提交未审查树
async def test_commit_rejects_index_changed_before_final_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "one.txt"
    target.write_text("reviewed A\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "one.txt"], check=True)
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff("staged")
    head_before = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    original_acquire = service._acquire_index_lock
    mutated = False

    # 在 commit 的最后互斥边界前把真实 index 变成未审查内容 B
    async def mutate_before_index_lock() -> object:
        nonlocal mutated
        target.write_text("unreviewed B\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp_path), "add", "one.txt"], check=True)
        mutated = True
        return await original_acquire()

    monkeypatch.setattr(service, "_acquire_index_lock", mutate_before_index_lock)

    with pytest.raises(ChangeCenterError, match="changed after review"):
        await service.commit(
            "must reject unreviewed tree",
            expected_digest=str(reviewed["state_digest"]),
        )

    head_after = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    staged_content = subprocess.run(
        ["git", "-C", str(tmp_path), "show", ":one.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert mutated is True
    assert head_after == head_before
    assert staged_content == "unreviewed B\n"


# 功能：commit 在 HEAD 前移后发现 worktree 竞态时必须自动回滚分支
# 设计：在第二次提交窗口摘要前注入内容 B，断言 HEAD 恢复且已审查 A 仍留在 index
async def test_commit_rolls_back_head_when_worktree_changes_during_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "one.txt"
    target.write_text("reviewed A\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "one.txt"], check=True)
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff("staged")
    head_before = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    original_guard = service._stable_worktree_guard
    guard_calls = 0

    # 在提交完成后的第二次 guard 计算前写入未审查工作区内容
    async def mutate_before_post_commit_guard() -> str:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 2:
            target.write_text("unreviewed B\n", encoding="utf-8")
        return await original_guard()

    monkeypatch.setattr(
        service,
        "_stable_worktree_guard",
        mutate_before_post_commit_guard,
    )

    with pytest.raises(ChangeCenterError, match="rolled back"):
        await service.commit(
            "must rollback raced commit",
            expected_digest=str(reviewed["state_digest"]),
        )

    head_after = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    staged_content = subprocess.run(
        ["git", "-C", str(tmp_path), "show", ":one.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert guard_calls == 2
    assert head_after == head_before
    assert staged_content == "reviewed A\n"
    assert target.read_text(encoding="utf-8") == "unreviewed B\n"


# 功能：stage 与 commit 的审查令牌不能在 all 和 staged scope 之间互换
# 设计：同一仓库状态同时取得两种可见审查，交叉提交令牌以隔离验证 scope 域绑定
async def test_review_tokens_are_bound_to_action_scope(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "one.txt").write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "one.txt"], check=True)
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    all_review = await service.diff("all")
    staged_review = await service.diff("staged")

    with pytest.raises(ChangeCenterError, match="changed after review"):
        await service.stage(
            ["one.txt"],
            expected_digest=str(staged_review["state_digest"]),
        )
    with pytest.raises(ChangeCenterError, match="changed after review"):
        await service.commit(
            "wrong review scope",
            expected_digest=str(all_review["state_digest"]),
        )


# 功能：审查令牌绑定规范化后的完整可见 payload 而不只绑定 Git 状态
# 设计：保持仓库字节不变但篡改下一次可见 diff 文本，断言旧令牌无法授权 stage
async def test_review_token_rejects_changed_visible_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "one.txt").write_text("reviewed\n", encoding="utf-8")
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff("all")
    original_invoke = change_center_module.GitDiffTool.invoke

    # 只改变用户看到的载荷而保持底层仓库状态不变
    async def alter_visible_payload(
        tool: object,
        params: dict[str, object],
    ) -> object:
        result = await original_invoke(tool, params)  # type: ignore[arg-type]
        payload = json.loads(result.content)
        payload["diff"] = str(payload["diff"]) + "\n[altered presentation]\n"
        result.content = json.dumps(payload, ensure_ascii=False)
        return result

    monkeypatch.setattr(change_center_module.GitDiffTool, "invoke", alter_visible_payload)

    with pytest.raises(ChangeCenterError, match="changed after review"):
        await service.stage(
            ["one.txt"],
            expected_digest=str(reviewed["state_digest"]),
        )
    assert _diff_names(tmp_path, "--cached") == set()


# 功能：未跟踪文件在内部审查后只改变 executable mode 也必须使 stage 摘要失效
# 设计：在 stage 已复核旧令牌但尚未创建 index lock 时注入 chmod，覆盖真实 TOCTOU 窗口而非普通二次审查
@pytest.mark.skipif(os.name == "nt", reason="Windows Git does not preserve POSIX executable mode")
async def test_stage_rejects_untracked_executable_mode_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "new-script.sh"
    target.write_text("echo safe\n", encoding="utf-8")
    target.chmod(0o644)
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff("all")
    original_acquire = service._acquire_index_lock

    # 在 stage 内部可见 payload 复核完成后改变将被 Git 提交的 executable bit
    async def chmod_before_index_lock() -> object:
        target.chmod(0o755)
        return await original_acquire()

    monkeypatch.setattr(service, "_acquire_index_lock", chmod_before_index_lock)

    with pytest.raises(ChangeCenterError, match="changed after review"):
        await service.stage(
            ["new-script.sh"],
            expected_digest=str(reviewed["state_digest"]),
        )

    assert _diff_names(tmp_path, "--cached") == set()
    assert not (tmp_path / ".git" / "index.lock").exists()


# 功能：初始即为 executable 的未跟踪脚本在 stage 与 commit 后保持权威 100755 mode
# 设计：从可见 synthetic diff 到 index 和 commit tree 逐层核对 mode，证明摘要规范化未改变 Git 语义
@pytest.mark.skipif(os.name == "nt", reason="Windows Git does not preserve POSIX executable mode")
async def test_initial_executable_file_stages_and_commits_as_100755(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "new-script.sh"
    target.write_text("echo safe\n", encoding="utf-8")
    target.chmod(0o755)
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff("all")

    assert "new file mode 100755" in str(reviewed["diff"])
    staged = await service.stage(
        ["new-script.sh"],
        expected_digest=str(reviewed["state_digest"]),
    )
    index_entry = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-files", "--stage", "new-script.sh"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert index_entry.startswith("100755 ")

    committed = await service.commit(
        "add executable script",
        expected_digest=str(staged["state_digest"]),
    )
    tree_entry = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-tree", committed.commit, "new-script.sh"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert tree_entry.startswith("100755 blob ")


# 功能：POSIX 字面反斜杠路径保持原身份并在 stage 边界被明确拒绝
# 设计：审查真实 `a\\b.txt` 后核对 payload 未折叠成目录，再证明词法安全门禁 fail closed
@pytest.mark.skipif(os.name == "nt", reason="Windows file names cannot contain backslash")
async def test_stage_rejects_literal_backslash_git_path_without_aliasing(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    target = tmp_path / r"a\b.txt"
    target.write_text("literal\n", encoding="utf-8")
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff("all")

    assert [item["path"] for item in reviewed["files"]] == [r"a\b.txt"]
    with pytest.raises(ChangeCenterError, match="backslash file names"):
        await service.stage(
            [r"a\b.txt"],
            expected_digest=str(reviewed["state_digest"]),
        )
    assert _diff_names(tmp_path, "--cached") == set()


# 功能：子目录 workspace 存在仓库其他目录的 staged 内容时所有 stage 动作失败关闭
# 设计：分别制造 workspace 内未暂存修改和仓库根已暂存修改，确保不可见 index 内容不会被保留
async def test_subdirectory_workspace_rejects_outside_staged_changes(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "inside.txt"
    inside.write_text("inside base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "workspace/inside.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "workspace base"], check=True)
    inside.write_text("inside changed\n", encoding="utf-8")
    (tmp_path / "one.txt").write_text("outside staged\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "one.txt"], check=True)
    service = ChangeCenterService(WorkspaceBoundary(workspace))
    reviewed = await service.diff("all")

    with pytest.raises(ChangeCenterError, match="outside this workspace"):
        await service.stage(
            ["inside.txt"],
            expected_digest=str(reviewed["state_digest"]),
        )
    assert _diff_names(tmp_path, "--cached") == {"one.txt"}


# 功能：子目录 workspace 的 commit 结果文件严格来自真实 commit diff 并保持 workspace 相对名
# 设计：只提交子目录文件，再把服务返回值与 diff-tree 权威输出逐项对照
async def test_subdirectory_commit_files_match_real_commit_diff(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "inside.txt"
    inside.write_text("inside base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "workspace/inside.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "workspace base"], check=True)
    inside.write_text("inside changed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "workspace/inside.txt"], check=True)
    service = ChangeCenterService(WorkspaceBoundary(workspace))
    reviewed = await service.diff("staged")

    result = await service.commit(
        "subdirectory change",
        expected_digest=str(reviewed["state_digest"]),
    )
    actual = subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            result.commit,
        ],
        check=True,
        capture_output=True,
    ).stdout
    actual_workspace_files = tuple(
        os.fsdecode(path.removeprefix(b"workspace/"))
        for path in actual.split(b"\0")
        if path
    )

    assert result.files == actual_workspace_files == ("inside.txt",)


# 功能：审查后即使 HEAD SHA 不变，切换符号分支也会使提交令牌失效
# 设计：在相同 commit 上创建并切换新分支，隔离验证 exact symbolic ref 已进入摘要
async def test_commit_review_binds_exact_symbolic_reference(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "one.txt").write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "one.txt"], check=True)
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff("staged")
    subprocess.run(["git", "-C", str(tmp_path), "switch", "-qc", "alternate"], check=True)

    with pytest.raises(ChangeCenterError, match="changed after review"):
        await service.commit(
            "wrong branch",
            expected_digest=str(reviewed["state_digest"]),
        )


# 功能：Change Center 在 detached HEAD 上明确拒绝创建无法安全绑定分支的提交
# 设计：detach 后取得有效 staged 审查，断言在 commit-tree 或 update-ref 前给出专用错误
async def test_commit_rejects_detached_head_explicitly(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", "--detach"], check=True)
    (tmp_path / "one.txt").write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "one.txt"], check=True)
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff("staged")
    head_before = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with pytest.raises(ChangeCenterError, match="detached HEAD"):
        await service.commit(
            "detached forbidden",
            expected_digest=str(reviewed["state_digest"]),
        )
    assert subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == head_before


# 功能：临时 index stage 保留未选择条目的 assume-unchanged、skip-worktree 与 split-index 语义
# 设计：同时启用三类特殊状态后只修改普通文件，并比较 stage 前后的 Git 权威标签与 shared index
async def test_stage_preserves_special_index_semantics(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    selected = tmp_path / "selected.txt"
    selected.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "selected.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "selected base"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "update-index", "--assume-unchanged", "one.txt"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "update-index", "--skip-worktree", "two.txt"],
        check=True,
    )
    split = subprocess.run(
        ["git", "-C", str(tmp_path), "update-index", "--split-index"],
        capture_output=True,
    )
    if split.returncode != 0:
        pytest.skip("Git split-index is unavailable")
    before_flags = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-files", "-v", "one.txt", "two.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    selected.write_text("changed\n", encoding="utf-8")
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff("all")

    await service.stage(
        ["selected.txt"],
        expected_digest=str(reviewed["state_digest"]),
    )
    after_flags = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-files", "-v", "one.txt", "two.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    assert before_flags == after_flags
    assert any((tmp_path / ".git").glob("sharedindex.*"))
    assert _diff_names(tmp_path, "--cached") == {"selected.txt"}


# 功能：stage 保留符号链接的词法路径并把链接本身而非外部目标加入 index
# 设计：创建指向 workspace 外文件的未跟踪链接，核对 staged mode 与 blob 正文均属于链接对象
async def test_stage_preserves_symlink_lexical_name(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_repo(workspace)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret\n", encoding="utf-8")
    link = workspace / "external-link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable on this platform")
    service = ChangeCenterService(WorkspaceBoundary(workspace))
    reviewed = await service.diff("all")

    await service.stage(
        ["external-link.txt"],
        expected_digest=str(reviewed["state_digest"]),
    )
    staged = subprocess.run(
        ["git", "-C", str(workspace), "ls-files", "--stage", "external-link.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    blob = subprocess.run(
        ["git", "-C", str(workspace), "show", ":external-link.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert staged.startswith("120000 ")
    expected_target = os.readlink(link).replace("\\", "/").removeprefix("//?/")
    assert blob.replace("\\", "/").removeprefix("//?/") == expected_target


# 功能：全新未出生仓库可完成首次 diff、stage 和无 parent 的初始 commit
# 设计：从不存在 index 的 git init 状态开始走完整公开 API，并检查提交父列表和文件结果
async def test_unborn_repository_supports_initial_commit(tmp_path: Path) -> None:
    _init_unborn_repo(tmp_path)
    (tmp_path / "first.txt").write_text("first\n", encoding="utf-8")
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff("all")
    staged = await service.stage(
        ["first.txt"],
        expected_digest=str(reviewed["state_digest"]),
    )

    result = await service.commit(
        "initial commit",
        expected_digest=str(staged["state_digest"]),
    )
    parents = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-list", "--parents", "-n", "1", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()

    assert result.files == ("first.txt",)
    assert parents == [result.commit]
    assert _diff_names(tmp_path, "--cached") == set()


# 功能：已有仓库切换 orphan 分支后仍可安全完成新的根提交
# 设计：使用 git switch --orphan 建立另一未出生引用，再验证 exact ref 创建和无父提交
async def test_orphan_branch_supports_new_root_commit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "switch", "-q", "--orphan", "fresh"], check=True)
    (tmp_path / "fresh.txt").write_text("fresh\n", encoding="utf-8")
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff("all")
    selected = [str(item["path"]) for item in reviewed["files"]]
    staged = await service.stage(
        selected,
        expected_digest=str(reviewed["state_digest"]),
    )

    result = await service.commit(
        "orphan root",
        expected_digest=str(staged["state_digest"]),
    )
    parents = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-list", "--parents", "-n", "1", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()

    assert parents == [result.commit]
    assert subprocess.run(
        ["git", "-C", str(tmp_path), "symbolic-ref", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "fresh"


# 功能：update-ref 已成功但调用协程收到取消时仍会探测并 CAS 回滚分支
# 设计：包装首次 update-ref 在真实写入后抛 CancelledError，核对原 HEAD 和 staged 内容均恢复
async def test_commit_cancellation_after_update_ref_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "one.txt").write_text("reviewed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "one.txt"], check=True)
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff("staged")
    head_before = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    original_run = service._run
    cancelled = False

    # 模拟 update-ref 已原子落盘但响应在返回调用方前被取消
    async def cancel_after_reference_update(
        *args: str,
        environment_overrides: dict[str, str] | None = None,
        allowed_returncodes: frozenset[int] = frozenset({0}),
    ) -> object:
        nonlocal cancelled
        result = await original_run(
            *args,
            environment_overrides=environment_overrides,
            allowed_returncodes=allowed_returncodes,
        )
        if args[:1] == ("update-ref",) and not cancelled:
            cancelled = True
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(service, "_run", cancel_after_reference_update)

    with pytest.raises(asyncio.CancelledError):
        await service.commit(
            "cancelled commit",
            expected_digest=str(reviewed["state_digest"]),
        )
    assert cancelled is True
    assert subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == head_before
    assert _diff_names(tmp_path, "--cached") == {"one.txt"}


# 功能：CAS 回滚绝不覆盖 update-ref 之后由并发方写入的第三个提交
# 设计：首次引用更新后用精确 CAS 注入 unrelated commit 并模拟丢响应，断言服务 fail closed 且保留并发值
async def test_commit_rollback_refuses_to_overwrite_unrelated_cas_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "one.txt").write_text("reviewed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "one.txt"], check=True)
    service = ChangeCenterService(WorkspaceBoundary(tmp_path))
    reviewed = await service.diff("staged")
    original_head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    original_tree = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    unrelated = subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "commit-tree",
            original_tree,
            "-p",
            original_head,
            "-m",
            "concurrent",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    branch = subprocess.run(
        ["git", "-C", str(tmp_path), "symbolic-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    original_run = service._run
    injected = False

    # 在服务 CAS 成功后由并发方再次 CAS，并让首个调用表现为响应丢失
    async def inject_unrelated_reference_update(
        *args: str,
        environment_overrides: dict[str, str] | None = None,
        allowed_returncodes: frozenset[int] = frozenset({0}),
    ) -> object:
        nonlocal injected
        result = await original_run(
            *args,
            environment_overrides=environment_overrides,
            allowed_returncodes=allowed_returncodes,
        )
        if args[:1] == ("update-ref",) and not injected:
            injected = True
            attempted = args[2]
            subprocess.run(
                ["git", "-C", str(tmp_path), "update-ref", branch, unrelated, attempted],
                check=True,
            )
            raise ChangeCenterError("simulated lost update-ref response")
        return result

    monkeypatch.setattr(service, "_run", inject_unrelated_reference_update)

    with pytest.raises(ChangeCenterError, match="unsafe.*rollback failed"):
        await service.commit(
            "raced commit",
            expected_digest=str(reviewed["state_digest"]),
        )
    assert injected is True
    assert subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == unrelated
