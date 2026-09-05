from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_rook.benchmark.polyglot import (
    load_polyglot_exercise,
    load_polyglot_tasks,
    polyglot_dataset_commit,
    verified_polyglot_isolation,
)


# 创建最小官方目录形状与 Git 提交，避免测试下载公开数据集
def _polyglot_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "polyglot"
    exercise = root / "python" / "exercises" / "practice" / "hello-world"
    (exercise / ".meta").mkdir(parents=True)
    (exercise / ".docs").mkdir()
    (exercise / ".meta" / "config.json").write_text(
        json.dumps(
            {
                "files": {
                    "solution": ["hello_world.py"],
                    "test": ["hello_world_test.py"],
                    "example": ["hello_world_example.py"],
                }
            }
        ),
        encoding="utf-8",
    )
    (exercise / ".docs" / "introduction.md").write_text("Intro.\n", encoding="utf-8")
    (exercise / ".docs" / "instructions.md").write_text("Return hello.\n", encoding="utf-8")
    (exercise / "hello_world.py").write_text("def hello():\n    return ''\n", encoding="utf-8")
    (exercise / "hello_world_test.py").write_text(
        "from hello_world import hello\n\ndef test_hello():\n    assert hello() == 'Hello'\n",
        encoding="utf-8",
    )
    (exercise / "hello_world_example.py").write_text("# example\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "tests@coderook.local"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "CodeRook Tests"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=root, check=True)
    commit = polyglot_dataset_commit(root)
    return root, exercise, commit


# 功能：验证官方 config 与 docs 被转换为受限 CodeRook 任务及 Python verifier
# 设计：构造 solution/test/example 三类文件，断言只允许改 solution 且 prompt 保持官方拼接语义
def test_load_polyglot_exercise_preserves_official_contract(tmp_path: Path) -> None:
    _root, exercise, commit = _polyglot_repo(tmp_path)

    loaded = load_polyglot_exercise(
        exercise,
        language="python",
        dataset_commit=commit,
    )

    assert loaded.task.id == "python-hello-world"
    assert loaded.task.allowed_change_paths == ["hello_world.py"]
    assert "hello_world_test.py" in loaded.task.forbidden_paths
    assert "Return hello." in loaded.task.goal
    assert "modify the supplied files: hello_world.py" in loaded.task.goal
    assert loaded.task.verifiers[0].argv == ["{python}", "-m", "pytest"]


# 功能：验证目录加载器绑定精确 commit，并支持语言、关键字和数量切片
# 设计：对单项 fixture 使用全部筛选条件，再用错误 commit 证明结果不会漂移到其他数据版本
def test_load_polyglot_tasks_requires_pinned_clean_dataset(tmp_path: Path) -> None:
    root, _exercise, commit = _polyglot_repo(tmp_path)

    tasks = load_polyglot_tasks(
        root,
        expected_commit=commit,
        languages={"python"},
        keywords=["hello"],
        limit=1,
    )

    assert [task.task.id for task in tasks] == ["python-hello-world"]
    with pytest.raises(ValueError, match="commit mismatch"):
        load_polyglot_tasks(root, expected_commit="0" * 40)


# 功能：验证数据集出现未提交修改时拒绝生成带伪 commit 的公开报告
# 设计：在已提交 fixture 中追加内容但不提交，断言 clean-check 先于任务执行失败
def test_polyglot_dataset_commit_rejects_dirty_checkout(tmp_path: Path) -> None:
    root, exercise, _commit = _polyglot_repo(tmp_path)
    (exercise / "hello_world.py").write_text("changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must be clean"):
        polyglot_dataset_commit(root)


# 功能：验证 config 中的目录穿越路径在复制或执行前即被拒绝
# 设计：把 solution 替换为父目录路径，覆盖来自不受信数据集元数据的路径边界
def test_load_polyglot_exercise_rejects_unsafe_config_paths(tmp_path: Path) -> None:
    _root, exercise, commit = _polyglot_repo(tmp_path)
    config_path = exercise / ".meta" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["files"]["solution"] = ["../escape.py"]
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="unsafe path"):
        load_polyglot_exercise(
            exercise,
            language="python",
            dataset_commit=commit,
        )


# 功能：验证 WSL2 公开评测只有在真实 bwrap 命名空间探针通过后才记录隔离环境
# 设计：替换平台、可执行路径与探针进程，覆盖环境声明到报告标签的最小可信链路
def test_verified_polyglot_isolation_accepts_probed_wsl2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("code_rook.benchmark.polyglot.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "code_rook.benchmark.polyglot.platform.release",
        lambda: "6.6.0-microsoft-standard-WSL2",
    )
    monkeypatch.setattr("code_rook.benchmark.polyglot.shutil.which", lambda _name: "/bin/bwrap")
    monkeypatch.setattr(
        "code_rook.benchmark.polyglot.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    assert verified_polyglot_isolation(
        {"CODEROOK_BENCHMARK_ISOLATION": "wsl2_bwrap"}
    ) == ("wsl2-linux", "bwrap-user-namespace")
