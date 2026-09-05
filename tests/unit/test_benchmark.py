from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from scripts.run_benchmark import _build_parser

from code_rook.benchmark.contract import (
    find_candidate_contract_issues,
    require_candidate_contract,
)
from code_rook.benchmark.executor import _prepare_git_workspace
from code_rook.benchmark.loader import (
    BenchmarkManifestError,
    LoadedBenchmarkTask,
    load_benchmark_tasks,
    validate_benchmark_catalog,
)
from code_rook.benchmark.models import AgentExecution, BenchmarkRunConfig, BenchmarkTask
from code_rook.benchmark.runner import BenchmarkRunner, verify_benchmark_baseline


# 功能：验证 benchmark 命令帮助可以正常渲染并保留百分号说明
# 设计：直接调用 argparse 格式化入口，覆盖未转义百分号导致 --help 崩溃的真实回归
def test_benchmark_cli_help_renders() -> None:
    help_text = _build_parser().format_help()

    assert "without gating on 100%" in help_text
    assert "task success." in help_text


# 功能：多 Agent 基准 fixture 会获得可创建 Worktree 的干净基线提交
# 设计：在临时目录连续初始化两次并检查 HEAD 与 porcelain，覆盖幂等和无脏文件约束
def test_prepare_git_workspace_creates_clean_idempotent_baseline(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("value = 1\n", encoding="utf-8")

    _prepare_git_workspace(tmp_path)
    first_head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    _prepare_git_workspace(tmp_path)
    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout

    assert len(first_head) == 40
    assert status == ""


class _EditingExecutor:
    # 保存要写入的相对路径和内容，构造确定性的基准执行结果
    def __init__(self, path: str, content: str) -> None:
        self._path = path
        self._content = content

    # 修改 fixture 副本并返回成功终态，避免单元测试依赖真实模型
    async def execute(
        self,
        task: BenchmarkTask,
        workspace: Path,
        runs_dir: Path,
    ) -> AgentExecution:
        del task, runs_dir
        target = workspace / self._path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self._content, encoding="utf-8")
        return AgentExecution(
            run_id="stub-run",
            status="success",
            steps=1,
            diagnostic_durations_ms=[10, 20],
            process_usage_records=2,
            complete_process_records=1,
            process_wall_ms=40,
            process_cpu_ms=12,
            peak_memory_bytes=8 * 1024 * 1024,
            process_count=3,
        )


# 构造只调用 Python 标准库的测试任务，保持评分测试快速且跨平台
def _loaded_task(tmp_path: Path) -> LoadedBenchmarkTask:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "answer.txt").write_text("wrong\n", encoding="utf-8")
    verifier = tmp_path / "verify.py"
    verifier.write_text(
        "from pathlib import Path\n"
        "raise SystemExit(0 if Path('answer.txt').read_text() == 'right\\n' else 1)\n",
        encoding="utf-8",
    )
    task = BenchmarkTask.model_validate(
        {
            "id": "stub-task",
            "title": "stub",
            "language": "text",
            "difficulty": "easy",
            "category": "single_file_fix",
            "suites": ["quick", "nightly", "release"],
            "baseline_commit": "fixture-v1",
            "fixture": "fixture",
            "goal": "write the expected answer",
            "allowed_tools": ["File"],
            "allowed_change_paths": ["answer.txt"],
            "forbidden_paths": ["protected.txt"],
            "budgets": {"max_steps": 8, "wall_time_s": 60},
            "verifiers": [
                {
                    "name": "answer",
                    "argv": ["{python}", str(verifier)],
                }
            ],
        }
    )
    return LoadedBenchmarkTask(
        task=task,
        manifest_path=tmp_path / "task.json",
        fixture_path=fixture,
    )


# 功能：验证清单加载器能解析仓库内 fixture 并保留任务元数据
# 设计：写入一份最小合法 JSON 后走公开加载入口，覆盖路径解析和 Pydantic 契约
def test_load_benchmark_tasks(tmp_path: Path) -> None:
    task_dir = tmp_path / "tasks"
    fixture = tmp_path / "fixtures" / "demo"
    task_dir.mkdir()
    fixture.mkdir(parents=True)
    (task_dir / "demo.json").write_text(
        """{
          "id": "demo", "title": "demo", "language": "python",
          "difficulty": "easy", "category": "single_file_fix",
          "schema_version": 1, "suites": ["quick", "nightly", "release"],
          "baseline_commit": "v1", "fixture": "fixtures/demo",
          "goal": "fix the demo", "allowed_tools": ["File"],
          "allowed_change_paths": ["demo.py"], "forbidden_paths": [],
          "budgets": {"max_steps": 8, "wall_time_s": 60},
          "verifiers": [{"name": "ok", "argv": ["{python}", "-V"]}]
        }""",
        encoding="utf-8",
    )

    loaded = load_benchmark_tasks(task_dir, tmp_path)

    assert loaded[0].task.id == "demo"
    assert loaded[0].fixture_path == fixture.resolve()


# 功能：验证仓库发布语料持续满足 40+ 总量、类别下限、10 项 quick 与全量套件合同
# 设计：直接加载真实 manifests 后执行目录级校验，防止单任务合法但整体配比悄然退化
def test_repository_benchmark_catalog_meets_release_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    tasks = load_benchmark_tasks(root / "benchmarks" / "tasks", root)

    validate_benchmark_catalog(tasks)

    assert len(tasks) == 50


# 功能：验证清单依赖默认值掩盖缺失预算时会在加载阶段明确失败
# 设计：构造字段不完整的独立清单并断言错误列出 budgets，锁定显式可审计契约
def test_benchmark_manifest_requires_explicit_contract_fields(tmp_path: Path) -> None:
    task_dir = tmp_path / "tasks"
    fixture = tmp_path / "fixture"
    task_dir.mkdir()
    fixture.mkdir()
    (task_dir / "incomplete.json").write_text(
        '{"id":"incomplete","fixture":"fixture"}',
        encoding="utf-8",
    )

    with pytest.raises(BenchmarkManifestError, match="budgets"):
        load_benchmark_tasks(task_dir, tmp_path)


# 功能：验证允许范围内的正确改动通过 verifier 后会被判为成功
# 设计：注入确定性编辑器写入预期内容，并用独立子进程 verifier 判断最终文件
async def test_benchmark_runner_passes_verified_change(tmp_path: Path) -> None:
    loaded = _loaded_task(tmp_path)
    runner = BenchmarkRunner(_EditingExecutor("answer.txt", "right\n"))

    result = await runner.run_task(loaded)

    assert result.passed is True
    assert result.failure_class is None
    assert [change.path for change in result.changes] == ["answer.txt"]


# 功能：验证受保护文件被修改时即使 Agent 宣称成功也必须判定失败
# 设计：注入越界编辑器新增 forbidden 文件，断言安全分类优先于 verifier 结果
async def test_benchmark_runner_rejects_forbidden_change(tmp_path: Path) -> None:
    loaded = _loaded_task(tmp_path)
    runner = BenchmarkRunner(_EditingExecutor("protected.txt", "changed\n"))

    result = await runner.run_task(loaded)

    assert result.passed is False
    assert result.failure_class == "forbidden_change"
    assert result.forbidden_changes == ["protected.txt"]


# 功能：验证基准报告汇总类别、验证率与任务 receipt，并把证据写入稳定输出目录
# 设计：运行单项确定性成功任务，检查 JSON 汇总字段及临时工作区销毁后仍存在的 receipt
async def test_benchmark_report_summarizes_and_preserves_evidence(tmp_path: Path) -> None:
    loaded = _loaded_task(tmp_path)
    evidence = tmp_path / "evidence"
    runner = BenchmarkRunner(
        _EditingExecutor("answer.txt", "right\n"),
        evidence_root=evidence,
    )

    report = await runner.run([loaded], repository_commit="abc123", suite="quick")

    assert report.summary.pass_rate == 1.0
    assert report.summary.verifier_pass_rate == 1.0
    assert report.summary.categories["single_file_fix"].passed == 1
    assert report.summary.diagnostics_p95_ms == 20
    assert report.summary.process_wall_p95_ms == 40
    assert report.summary.process_cpu_p95_ms == 12
    assert report.summary.peak_memory_p95_bytes == 8 * 1024 * 1024
    assert report.summary.process_count_p95 == 3
    assert report.summary.process_usage_complete_rate == 0.5
    assert report.run_config.task_count == 1
    assert len(report.run_config.task_catalog_fingerprint) == 64
    assert len(report.run_config.fixture_fingerprint) == 64
    assert len(report.run_config.budget_fingerprint) == 64
    assert report.task_contracts[0].budgets.max_steps == 8
    assert report.task_contracts[0].allowed_tools == ["File"]
    receipt = evidence / "stub-task" / "receipt.json"
    assert receipt.is_file()
    assert '"task_id": "stub-task"' in receipt.read_text(encoding="utf-8")


# 功能：验证基线检查能识别尚未修复且 verifier 失败的 fixture
# 设计：复用最小任务的错误初始内容，直接走临时副本验证入口以排除 Agent 影响
async def test_verify_benchmark_baseline_requires_failure(tmp_path: Path) -> None:
    loaded = _loaded_task(tmp_path)

    results = await verify_benchmark_baseline(loaded)

    assert len(results) == 1
    assert results[0].passed is False


# 功能：验证真实候选合同绑定完整 commit、route、配置、任务、fixture 与预算指纹
# 设计：用确定性执行器生成有效报告再篡改 candidate hash，覆盖成功合同与防人工改报告两个分支
async def test_candidate_contract_rejects_unknown_or_tampered_identity(
    tmp_path: Path,
) -> None:
    loaded = _loaded_task(tmp_path)
    runner = BenchmarkRunner(_EditingExecutor("answer.txt", "right\n"))
    valid_config = BenchmarkRunConfig(
        route_id="fixed-route",
        model="fixed-model",
        wire_format="openai_responses",
        config_fingerprint="a" * 64,
    )
    report = await runner.run(
        [loaded],
        repository_commit="b" * 40,
        suite="release",
        run_config=valid_config,
    )

    require_candidate_contract(report)
    assert find_candidate_contract_issues(report) == []

    tampered_config = report.run_config.model_copy(
        update={"candidate_fingerprint": "c" * 64}
    )
    tampered = report.model_copy(update={"run_config": tampered_config})
    assert "run_config.candidate_fingerprint does not match report material" in (
        find_candidate_contract_issues(tampered)
    )

    unknown = await runner.run([loaded], repository_commit="abc123", suite="quick")
    with pytest.raises(ValueError, match="repository_commit"):
        require_candidate_contract(unknown)
