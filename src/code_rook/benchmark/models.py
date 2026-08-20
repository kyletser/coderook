from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

BenchmarkSuite = Literal["quick", "nightly", "release"]


# 返回默认覆盖夜间与发布门禁的基准套件集合
def _default_suites() -> set[BenchmarkSuite]:
    return {"nightly", "release"}


class BenchmarkBudgets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps: int = Field(default=20, ge=1)
    wall_time_s: float = Field(default=600.0, gt=0)
    max_total_tokens: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)


class VerifierSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    argv: list[str] = Field(min_length=1)
    timeout_s: float = Field(default=120.0, gt=0)
    cwd: str = "."

    # 拒绝空参数，避免把无效命令推迟到执行阶段才暴露
    @field_validator("argv")
    @classmethod
    def _validate_argv(cls, value: list[str]) -> list[str]:
        if any(not part for part in value):
            raise ValueError("verifier argv entries must not be empty")
        return value


class BenchmarkTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1)
    language: str = Field(min_length=1)
    difficulty: Literal["easy", "medium", "hard"]
    category: Literal[
        "explain",
        "single_file_fix",
        "multi_file_change",
        "test_and_verify",
        "refactor",
        "security_negative",
    ]
    suites: set[BenchmarkSuite] = Field(
        default_factory=_default_suites,
        min_length=1,
    )
    baseline_commit: str = Field(min_length=1)
    fixture: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    allowed_tools: list[str] = Field(min_length=1)
    allowed_change_paths: list[str] = Field(default_factory=lambda: ["**"])
    forbidden_paths: list[str] = Field(default_factory=list)
    budgets: BenchmarkBudgets = Field(default_factory=BenchmarkBudgets)
    verifiers: list[VerifierSpec] = Field(min_length=1)

    # 保证所有路径规则都是相对规则，防止清单把审计范围指向工作区外
    @field_validator("fixture", "allowed_change_paths", "forbidden_paths")
    @classmethod
    def _validate_relative_paths(cls, value: str | list[str]) -> str | list[str]:
        values = [value] if isinstance(value, str) else value
        if any(item.startswith(("/", "\\")) or ":" in item for item in values):
            raise ValueError("benchmark paths must be relative")
        if any(".." in item.replace("\\", "/").split("/") for item in values):
            raise ValueError("benchmark paths must not contain '..'")
        return value


class AgentExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    status: str
    result: str = ""
    reason: str | None = None
    route_id: str = ""
    model: str = ""
    wire_format: str = ""
    temperature: float | None = None
    elapsed_s: float = Field(default=0.0, ge=0)
    steps: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    pricing_evidence: list[str] = Field(default_factory=list)
    approval_requests: int = 0
    rollback_count: int = 0
    retry_count: int = 0
    compaction_count: int = 0
    daemon_restart_count: int = 0
    diagnostic_durations_ms: list[int] = Field(default_factory=list)
    process_usage_records: int = Field(default=0, ge=0)
    complete_process_records: int = Field(default=0, ge=0)
    process_wall_ms: int = Field(default=0, ge=0)
    process_cpu_ms: int = Field(default=0, ge=0)
    peak_memory_bytes: int = Field(default=0, ge=0)
    process_count: int = Field(default=0, ge=0)
    first_edit_correct: bool | None = None
    timed_out: bool = False


class FileChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    kind: Literal["added", "modified", "deleted"]


class VerifierResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    argv: list[str]
    exit_code: int | None
    elapsed_s: float
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    # 返回验证命令是否在时限内正常退出且状态为零
    @property
    def passed(self) -> bool:
        return not self.timed_out and self.exit_code == 0


class BenchmarkTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    title: str
    category: str
    passed: bool
    failure_class: str | None = None
    execution: AgentExecution
    changes: list[FileChange] = Field(default_factory=list)
    forbidden_changes: list[str] = Field(default_factory=list)
    unexpected_changes: list[str] = Field(default_factory=list)
    verifiers: list[VerifierResult] = Field(default_factory=list)
    evidence_path: str = ""


class BenchmarkCategorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int
    passed: int
    pass_rate: float


class BenchmarkSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int
    passed: int
    pass_rate: float
    verifier_pass_rate: float
    first_edit_correct_rate: float | None = None
    first_edit_known: int = 0
    non_target_file_changes: int = 0
    approval_requests: int = 0
    rollbacks: int = 0
    retries: int = 0
    compactions: int = 0
    daemon_restarts: int = 0
    total_tokens: int = 0
    elapsed_p50_s: float | None = None
    elapsed_p95_s: float | None = None
    cost_p50_usd: float | None = None
    cost_p95_usd: float | None = None
    diagnostics_p95_ms: float | None = None
    process_wall_p95_ms: float | None = None
    process_cpu_p95_ms: float | None = None
    peak_memory_p95_bytes: float | None = None
    process_count_p95: float | None = None
    process_usage_complete_rate: float | None = None
    categories: dict[str, BenchmarkCategorySummary] = Field(default_factory=dict)


class BenchmarkRunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route_id: str = "unknown"
    model: str = "unknown"
    wire_format: str = "unknown"
    router: str = "static"
    thinking: str = "off"
    temperature: float | None = None
    config_fingerprint: str = "unknown"
    benchmark_name: str = "coderook-50"
    dataset_name: str = "benchmarks/fixtures/coding-katas-v1"
    dataset_commit: str = "unknown"
    task_count: int = Field(default=0, ge=0)
    task_catalog_fingerprint: str = "unknown"
    fixture_fingerprint: str = "unknown"
    budget_fingerprint: str = "unknown"
    candidate_fingerprint: str = "unknown"


class BenchmarkTaskContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    baseline_commit: str
    allowed_tools: list[str]
    budgets: BenchmarkBudgets
    task_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class BenchmarkReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    generated_at: str
    repository_commit: str
    suite: str | None = None
    results: list[BenchmarkTaskResult]
    summary: BenchmarkSummary
    run_config: BenchmarkRunConfig = Field(default_factory=BenchmarkRunConfig)
    task_contracts: list[BenchmarkTaskContract] = Field(default_factory=list)

    # 计算报告中的任务总数
    @property
    def total(self) -> int:
        return len(self.results)

    # 计算报告中的通过任务数
    @property
    def passed(self) -> int:
        return sum(result.passed for result in self.results)

    # 计算本次基准的 pass@1 比例
    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0
