from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class SWEbenchInstance(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]+$")
    repo: str = Field(min_length=1)
    base_commit: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")
    problem_statement: str = Field(min_length=1)


class SWEbenchPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    model_name_or_path: str = Field(min_length=1)
    model_patch: str


# 读取官方数据集导出的 JSON 或 JSONL，并拒绝重复实例编号
def load_swebench_instances(path: Path) -> list[SWEbenchInstance]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        raw_items = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif path.suffix.lower() == ".json":
        loaded = json.loads(text)
        if not isinstance(loaded, list):
            raise ValueError("SWE-bench JSON dataset must contain a list")
        raw_items = loaded
    else:
        raise ValueError("SWE-bench instances path must end with .json or .jsonl")
    instances = [SWEbenchInstance.model_validate(item) for item in raw_items]
    ids = [instance.instance_id for instance in instances]
    duplicate_ids = sorted({instance_id for instance_id in ids if ids.count(instance_id) > 1})
    if duplicate_ids:
        raise ValueError("duplicate SWE-bench instance id(s): " + ", ".join(duplicate_ids))
    return instances


# 运行 Git 命令并把失败转成包含 stderr 的可诊断异常
def _run_git(
    workspace: Path,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *argv],
        cwd=workspace,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise RuntimeError(f"git {' '.join(argv)} failed: {detail}")
    return result


# 确认工作区属于目标基线提交，避免把其他版本的差异误交给官方 harness
def validate_swebench_workspace(instance: SWEbenchInstance, workspace: Path) -> None:
    if not workspace.is_dir():
        raise ValueError(f"workspace does not exist: {workspace}")
    root = Path(_run_git(workspace, ["rev-parse", "--show-toplevel"]).stdout.strip()).resolve()
    if root != workspace.resolve():
        raise ValueError(f"workspace must be the Git root: {workspace}")
    head = _run_git(workspace, ["rev-parse", "HEAD"]).stdout.strip()
    expected = _run_git(workspace, ["rev-parse", instance.base_commit]).stdout.strip()
    if head != expected:
        raise ValueError(
            f"workspace HEAD {head} does not match {instance.instance_id} "
            f"base_commit {expected}"
        )


# 使用临时 Git index 生成同时包含 tracked 与 untracked 文件的标准二进制 patch
def build_swebench_patch(instance: SWEbenchInstance, workspace: Path) -> str:
    validate_swebench_workspace(instance, workspace)
    git_dir_text = _run_git(workspace, ["rev-parse", "--git-dir"]).stdout.strip()
    git_dir = Path(git_dir_text)
    if not git_dir.is_absolute():
        git_dir = (workspace / git_dir).resolve()
    source_index = git_dir / "index"
    with tempfile.TemporaryDirectory(prefix="coderook-swebench-index-") as temp_dir:
        temp_index = Path(temp_dir) / "index"
        if source_index.is_file():
            shutil.copyfile(source_index, temp_index)
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(temp_index)
        _run_git(workspace, ["add", "--intent-to-add", "--all", "--", "."], env=env)
        result = _run_git(
            workspace,
            [
                "diff",
                "--binary",
                "--no-ext-diff",
                "--no-color",
                instance.base_commit,
                "--",
                ".",
            ],
            env=env,
        )
    return result.stdout


# 从一个已由 CodeRook 修改的基线工作区构造官方三字段 prediction
def build_swebench_prediction(
    instance: SWEbenchInstance,
    workspace: Path,
    model_name_or_path: str,
) -> SWEbenchPrediction:
    return SWEbenchPrediction(
        instance_id=instance.instance_id,
        model_name_or_path=model_name_or_path,
        model_patch=build_swebench_patch(instance, workspace),
    )


# 按官方 harness 支持的 JSONL 格式写出 predictions
def write_swebench_predictions(
    predictions: list[SWEbenchPrediction],
    path: Path,
) -> None:
    ids = [prediction.instance_id for prediction in predictions]
    if len(ids) != len(set(ids)):
        raise ValueError("SWE-bench predictions contain duplicate instance ids")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [prediction.model_dump_json() for prediction in predictions]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


# 生成可直接交给官方 swebench.harness.run_evaluation 的参数数组
def build_swebench_harness_command(
    predictions_path: Path,
    *,
    dataset_name: str,
    split: str,
    run_id: str,
    max_workers: int,
    instance_ids: list[str] | None = None,
) -> list[str]:
    if max_workers < 1:
        raise ValueError("max_workers must be at least one")
    command = [
        "python",
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        dataset_name,
        "--split",
        split,
        "--predictions_path",
        str(predictions_path),
        "--max_workers",
        str(max_workers),
        "--run_id",
        run_id,
    ]
    if instance_ids:
        command.extend(["--instance_ids", *instance_ids])
    return command
