from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from code_rook.benchmark.loader import LoadedBenchmarkTask
from code_rook.benchmark.models import BenchmarkBudgets, BenchmarkTask, VerifierSpec

_INSTRUCTIONS_ADDENDUM = (
    "\n\n#### Use the above instructions to modify the supplied files: {file_list}\n"
    "Don't change the names of existing functions or classes, as they may be referenced "
    "from other code like unit tests, etc. Only use standard libraries, don't suggest "
    "installing any packages."
)
_IGNORED_SOLUTION_FILES = {"CMakeLists.txt", "Cargo.toml"}
_DIRECT_TEST_COMMANDS: dict[str, list[str]] = {
    ".py": ["{python}", "-m", "pytest"],
    ".rs": ["cargo", "test", "--", "--include-ignored"],
    ".go": ["go", "test", "./..."],
    ".java": ["./gradlew", "test"],
}


# 读取 Git 数据集当前提交并拒绝未提交修改，保证公开结果可以复现
def polyglot_dataset_commit(root: Path) -> str:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    if status:
        raise ValueError("Aider Polyglot dataset checkout must be clean")
    return head


# 校验来自 config.json 的相对路径不会逃逸 exercise 工作区
def _safe_paths(raw: object, field: str) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"polyglot config files.{field} must be a list of paths")
    values = [str(item).replace("\\", "/") for item in raw]
    if any(
        not value
        or value.startswith("/")
        or ":" in value
        or ".." in value.split("/")
        for value in values
    ):
        raise ValueError(f"polyglot config files.{field} contains an unsafe path")
    return values


# 复刻 Aider harness 的 introduction/instructions/append 拼接顺序和文件约束附言
def _exercise_goal(exercise: Path, solution_files: list[str]) -> str:
    sections: list[str] = []
    introduction = exercise / ".docs" / "introduction.md"
    instructions = exercise / ".docs" / "instructions.md"
    append = exercise / ".docs" / "instructions.append.md"
    if introduction.is_file():
        sections.append(introduction.read_text(encoding="utf-8"))
    if not instructions.is_file():
        raise ValueError(f"missing Polyglot instructions: {instructions}")
    sections.append(instructions.read_text(encoding="utf-8"))
    if append.is_file():
        sections.append(append.read_text(encoding="utf-8"))
    body = "".join(sections).rstrip()
    file_list = " ".join(Path(path).name for path in solution_files)
    return body + _INSTRUCTIONS_ADDENDUM.format(file_list=file_list)


# 按官方扩展名映射选择测试命令，JS/C++ 使用 Aider 容器中的辅助脚本
def _test_command(
    test_files: list[str],
    aider_benchmark_dir: Path | None,
) -> list[str]:
    extensions = sorted({Path(path).suffix for path in test_files})
    candidates = [extension for extension in extensions if extension in _DIRECT_TEST_COMMANDS]
    if ".js" in extensions:
        candidates.append(".js")
    if ".cpp" in extensions:
        candidates.append(".cpp")
    if len(candidates) != 1:
        raise ValueError(
            "Polyglot exercise must map to exactly one supported test command; "
            f"extensions={extensions}"
        )
    extension = candidates[0]
    if extension in _DIRECT_TEST_COMMANDS:
        return list(_DIRECT_TEST_COMMANDS[extension])
    if aider_benchmark_dir is None:
        raise ValueError(f"{extension} exercises require --aider-benchmark-dir")
    helper_name = "npm-test.sh" if extension == ".js" else "cpp-test.sh"
    helper = (aider_benchmark_dir / helper_name).resolve()
    if not helper.is_file():
        raise ValueError(f"missing Aider benchmark helper: {helper}")
    return [str(helper)]


# 将单个官方 Polyglot exercise 转成 CodeRook 的固定任务与 verifier 契约
def load_polyglot_exercise(
    exercise: Path,
    *,
    language: str,
    dataset_commit: str,
    aider_benchmark_dir: Path | None = None,
    max_steps: int = 20,
    wall_time_s: float = 600.0,
    max_cost_usd: float | None = None,
) -> LoadedBenchmarkTask:
    config_path = exercise / ".meta" / "config.json"
    if not config_path.is_file():
        raise ValueError(f"missing Polyglot config: {config_path}")
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw_config, dict) or not isinstance(raw_config.get("files"), dict):
        raise ValueError(f"invalid Polyglot config: {config_path}")
    files = raw_config["files"]
    solution_files = _safe_paths(files.get("solution"), "solution")
    test_files = _safe_paths(files.get("test"), "test")
    example_files = _safe_paths(files.get("example", []), "example")
    protected = set(test_files) | set(example_files) | _IGNORED_SOLUTION_FILES
    editable = sorted(path for path in solution_files if path not in protected)
    if not editable:
        raise ValueError(f"Polyglot exercise has no editable solution files: {exercise}")
    missing = sorted(path for path in [*editable, *test_files] if not (exercise / path).is_file())
    if missing:
        raise ValueError(
            f"Polyglot exercise references missing file(s): {', '.join(missing)}"
        )
    task_id = re.sub(r"[^a-z0-9_-]+", "-", f"{language}-{exercise.name}".lower())
    return LoadedBenchmarkTask(
        task=BenchmarkTask(
            id=task_id,
            title=f"Aider Polyglot: {language}/{exercise.name}",
            language=language,
            difficulty="medium",
            category="single_file_fix" if len(editable) == 1 else "multi_file_change",
            suites={"release"},
            baseline_commit=dataset_commit,
            fixture=f"{language}/exercises/practice/{exercise.name}",
            goal=_exercise_goal(exercise, editable),
            allowed_tools=["File", "Git", "Run", "Bash"],
            allowed_change_paths=editable,
            forbidden_paths=sorted(
                {
                    *test_files,
                    *example_files,
                    ".docs/**",
                    ".meta/**",
                    "CMakeLists.txt",
                    "Cargo.toml",
                }
            ),
            budgets=BenchmarkBudgets(
                max_steps=max_steps,
                wall_time_s=wall_time_s,
                max_cost_usd=max_cost_usd,
            ),
            verifiers=[
                VerifierSpec(
                    name="aider-polyglot-tests",
                    argv=_test_command(test_files, aider_benchmark_dir),
                    timeout_s=180.0,
                )
            ],
        ),
        manifest_path=config_path,
        fixture_path=exercise.resolve(),
    )


# 枚举官方六语言目录并按语言、关键字和数量构造固定任务切片
def load_polyglot_tasks(
    root: Path,
    *,
    expected_commit: str,
    languages: set[str] | None = None,
    keywords: list[str] | None = None,
    limit: int | None = None,
    aider_benchmark_dir: Path | None = None,
    max_steps: int = 20,
    wall_time_s: float = 600.0,
    max_cost_usd: float | None = None,
) -> list[LoadedBenchmarkTask]:
    commit = polyglot_dataset_commit(root)
    if commit != expected_commit:
        raise ValueError(
            f"Aider Polyglot commit mismatch: expected {expected_commit}, found {commit}"
        )
    selected_languages = {value.lower() for value in languages} if languages else None
    exercises: list[tuple[str, Path]] = []
    for language_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        language = language_dir.name.lower()
        if selected_languages is not None and language not in selected_languages:
            continue
        practice = language_dir / "exercises" / "practice"
        if practice.is_dir():
            exercises.extend(
                (language, path)
                for path in sorted(practice.iterdir())
                if path.is_dir()
            )
    if keywords:
        exercises = [
            item
            for item in exercises
            if any(keyword.lower() in f"{item[0]}/{item[1].name}".lower() for keyword in keywords)
        ]
    if limit is not None:
        if limit < 1:
            raise ValueError("Polyglot limit must be at least one")
        exercises = exercises[:limit]
    if not exercises:
        raise ValueError("no Aider Polyglot exercises matched the selection")
    return [
        load_polyglot_exercise(
            exercise,
            language=language,
            dataset_commit=commit,
            aider_benchmark_dir=aider_benchmark_dir,
            max_steps=max_steps,
            wall_time_s=wall_time_s,
            max_cost_usd=max_cost_usd,
        )
        for language, exercise in exercises
    ]
