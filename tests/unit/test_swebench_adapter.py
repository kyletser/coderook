from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from code_rook.benchmark.swebench import (
    SWEbenchInstance,
    build_swebench_harness_command,
    build_swebench_prediction,
    load_swebench_instances,
    validate_swebench_workspace,
    write_swebench_predictions,
)


# 在临时目录初始化一个无需用户级 Git 配置的单提交仓库
def _repository(tmp_path: Path) -> tuple[Path, str]:
    workspace = tmp_path / "demo__repo-1"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@coderook.local"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "CodeRook Tests"],
        cwd=workspace,
        check=True,
    )
    (workspace / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=workspace, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return workspace, commit


# 构造与官方 Lite/Verified 数据字段相同的最小实例
def _instance(commit: str) -> SWEbenchInstance:
    return SWEbenchInstance(
        instance_id="demo__repo-1",
        repo="demo/repo",
        base_commit=commit,
        problem_statement="Update tracked.py and add a regression test.",
    )


# 功能：验证适配器读取 JSONL 时保留官方字段并拒绝重复实例
# 设计：用同一条官方形状记录先单独加载再复制，覆盖正常解析和去重失败边界
def test_load_swebench_instances_accepts_official_jsonl_and_rejects_duplicates(
    tmp_path: Path,
) -> None:
    _workspace, commit = _repository(tmp_path)
    record = _instance(commit).model_dump_json()
    dataset = tmp_path / "instances.jsonl"
    dataset.write_text(record + "\n", encoding="utf-8")

    assert load_swebench_instances(dataset)[0].base_commit == commit

    dataset.write_text(record + "\n" + record + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate SWE-bench instance"):
        load_swebench_instances(dataset)


# 功能：验证标准 patch 同时包含已跟踪修改和未跟踪新增文件且不污染真实 index
# 设计：导出前后比较 staged diff，并断言 prediction 只有官方要求的三个字段
def test_build_swebench_prediction_includes_untracked_files_without_staging(
    tmp_path: Path,
) -> None:
    workspace, commit = _repository(tmp_path)
    (workspace / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    (workspace / "test_regression.py").write_text("assert True\n", encoding="utf-8")

    prediction = build_swebench_prediction(
        _instance(commit),
        workspace,
        "coderook/test-model",
    )

    assert "tracked.py" in prediction.model_patch
    assert "test_regression.py" in prediction.model_patch
    assert "value = 2" in prediction.model_patch
    assert set(prediction.model_dump()) == {
        "instance_id",
        "model_name_or_path",
        "model_patch",
    }
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert staged == ""


# 功能：验证工作区 HEAD 与实例基线不一致时拒绝生成不可评分 patch
# 设计：在 fixture 上追加提交后仍使用旧 base_commit，断言错误明确列出版本不匹配
def test_validate_swebench_workspace_rejects_wrong_head(tmp_path: Path) -> None:
    workspace, commit = _repository(tmp_path)
    (workspace / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "next"], cwd=workspace, check=True)

    with pytest.raises(ValueError, match="does not match"):
        validate_swebench_workspace(_instance(commit), workspace)


# 功能：验证 JSONL prediction 可由官方三字段逐行解析且 harness 参数完整
# 设计：写出单条 prediction 后用标准库读取，并检查官方模块、数据集、实例过滤和 worker 参数
def test_write_predictions_and_build_official_harness_command(tmp_path: Path) -> None:
    workspace, commit = _repository(tmp_path)
    (workspace / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    prediction = build_swebench_prediction(_instance(commit), workspace, "coderook")
    output = tmp_path / "predictions.jsonl"

    write_swebench_predictions([prediction], output)
    command = build_swebench_harness_command(
        output,
        dataset_name="princeton-nlp/SWE-bench_Lite",
        split="test",
        run_id="coderook-smoke",
        max_workers=2,
        instance_ids=[prediction.instance_id],
    )

    assert json.loads(output.read_text(encoding="utf-8")) == prediction.model_dump()
    assert command[:3] == ["python", "-m", "swebench.harness.run_evaluation"]
    assert command[command.index("--predictions_path") + 1] == str(output)
    assert command[-2:] == ["--instance_ids", "demo__repo-1"]
