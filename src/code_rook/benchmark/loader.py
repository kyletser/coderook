from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from code_rook.benchmark.models import BenchmarkTask


class BenchmarkManifestError(ValueError):
    """表示任务清单或 fixture 不满足基准契约。"""


@dataclass(frozen=True)
class LoadedBenchmarkTask:
    task: BenchmarkTask
    manifest_path: Path
    fixture_path: Path


_REQUIRED_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "title",
        "language",
        "difficulty",
        "category",
        "suites",
        "baseline_commit",
        "fixture",
        "goal",
        "allowed_tools",
        "allowed_change_paths",
        "forbidden_paths",
        "budgets",
        "verifiers",
    }
)
_MINIMUM_CATEGORY_COUNTS = {
    "explain": 6,
    "single_file_fix": 8,
    "multi_file_change": 10,
    "test_and_verify": 6,
    "refactor": 6,
    "security_negative": 4,
}


# 加载目录内全部 JSON 清单并校验任务编号和 fixture 路径
def load_benchmark_tasks(task_dir: Path, repository_root: Path) -> list[LoadedBenchmarkTask]:
    root = repository_root.resolve()
    manifests = sorted(task_dir.resolve().glob("*.json"))
    if not manifests:
        raise BenchmarkManifestError(f"no benchmark manifests found in {task_dir}")

    loaded: list[LoadedBenchmarkTask] = []
    seen_ids: set[str] = set()
    for manifest_path in manifests:
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("manifest root must be an object")
            missing = sorted(_REQUIRED_MANIFEST_FIELDS - raw.keys())
            if missing:
                raise ValueError("missing explicit fields: " + ", ".join(missing))
            task = BenchmarkTask.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise BenchmarkManifestError(f"invalid manifest {manifest_path}: {exc}") from exc
        if task.id in seen_ids:
            raise BenchmarkManifestError(f"duplicate benchmark task id: {task.id}")
        seen_ids.add(task.id)

        fixture_path = (root / task.fixture).resolve()
        if not fixture_path.is_relative_to(root) or not fixture_path.is_dir():
            raise BenchmarkManifestError(
                f"task {task.id} fixture is missing or outside repository: {task.fixture}"
            )
        loaded.append(
            LoadedBenchmarkTask(
                task=task,
                manifest_path=manifest_path,
                fixture_path=fixture_path,
            )
        )
    return loaded


# 校验发布语料的任务总数、类别配比、套件覆盖与语言覆盖满足 R0 合同
def validate_benchmark_catalog(tasks: list[LoadedBenchmarkTask]) -> None:
    if len(tasks) < 40:
        raise BenchmarkManifestError("benchmark catalog must contain at least 40 tasks")
    counts: Counter[str] = Counter(task.task.category for task in tasks)
    shortages = {
        category: minimum - counts[category]
        for category, minimum in _MINIMUM_CATEGORY_COUNTS.items()
        if counts[category] < minimum
    }
    if shortages:
        detail = ", ".join(
            f"{category} missing {missing}"
            for category, missing in sorted(shortages.items())
        )
        raise BenchmarkManifestError(f"benchmark category contract failed: {detail}")
    quick_count = sum("quick" in loaded.task.suites for loaded in tasks)
    if quick_count != 10:
        raise BenchmarkManifestError(
            f"quick benchmark suite must contain exactly 10 tasks, got {quick_count}"
        )
    for suite in ("nightly", "release"):
        missing = [loaded.task.id for loaded in tasks if suite not in loaded.task.suites]
        if missing:
            raise BenchmarkManifestError(
                f"{suite} suite must cover every task; missing: {', '.join(missing)}"
            )
    languages = {loaded.task.language for loaded in tasks}
    if not {"python", "typescript", "python+typescript"}.issubset(languages):
        raise BenchmarkManifestError(
            "benchmark catalog must cover Python, TypeScript and mixed repositories"
        )
