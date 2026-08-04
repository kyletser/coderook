from __future__ import annotations

import json

import pytest

from code_rook.core.workflow import (
    FanInStep,
    WorkflowParseError,
    parse_workflow_text,
)


# 返回覆盖全部 V1 控制结构的声明式 Workflow JSON 对象
def _complete_workflow() -> dict[str, object]:
    worker = {
        "type": "worker",
        "id": "prepare",
        "description": "prepare input",
        "prompt": "prepare the input",
    }
    return {
        "schema_version": 1,
        "id": "workflow-all",
        "name": "all constructs",
        "limits": {
            "max_nodes": 32,
            "max_depth": 8,
            "max_concurrency": 2,
            "token_budget": 10_000,
            "wall_time_s": 300,
        },
        "root": {
            "type": "sequence",
            "id": "root",
            "steps": [
                worker,
                {
                    "type": "parallel",
                    "id": "parallel",
                    "max_concurrency": 2,
                    "steps": [
                        {
                            "type": "worker",
                            "id": "parallel-a",
                            "description": "parallel a",
                            "prompt": "run a",
                        },
                        {
                            "type": "worker",
                            "id": "parallel-b",
                            "description": "parallel b",
                            "prompt": "run b",
                        },
                    ],
                },
                {
                    "type": "branch",
                    "id": "branch",
                    "condition": {
                        "source": "prepare",
                        "field": "status",
                        "operator": "eq",
                        "value": "completed",
                    },
                    "then_step": {
                        "type": "worker",
                        "id": "branch-yes",
                        "description": "yes branch",
                        "prompt": "run yes",
                    },
                    "else_step": {
                        "type": "worker",
                        "id": "branch-no",
                        "description": "no branch",
                        "prompt": "run no",
                    },
                },
                {
                    "type": "retry",
                    "id": "retry",
                    "max_attempts": 3,
                    "backoff_s": 0,
                    "step": {
                        "type": "worker",
                        "id": "retry-worker",
                        "description": "retry worker",
                        "prompt": "retry me",
                    },
                },
                {
                    "type": "review_gate",
                    "id": "review",
                    "step": {
                        "type": "worker",
                        "id": "risky-write",
                        "description": "risky write",
                        "prompt": "change release config",
                        "high_risk_write": True,
                        "write_claim": {
                            "read_only": False,
                            "exact_files": ["release.toml"],
                        },
                    },
                    "reviewer": {
                        "type": "worker",
                        "id": "reviewer",
                        "description": "review change",
                        "prompt": "review release config",
                        "profile": "reviewer",
                    },
                },
                {
                    "type": "fan_in",
                    "id": "fan-in",
                    "owner": "parent",
                    "steps": [
                        {
                            "type": "worker",
                            "id": "fan-a",
                            "description": "fan a",
                            "prompt": "collect a",
                        },
                        {
                            "type": "worker",
                            "id": "fan-b",
                            "description": "fan b",
                            "prompt": "collect b",
                        },
                    ],
                },
            ],
        },
    }


# 功能：JSON IR 可解析 sequence、parallel、branch、retry、review_gate 和 fan_in
# 设计：使用一个覆盖全部控制结构的配置并检查关键类型和限制字段
def test_json_workflow_parses_all_v1_constructs() -> None:
    spec = parse_workflow_text(json.dumps(_complete_workflow()), format="json")

    assert spec.id == "workflow-all"
    assert spec.root.type == "sequence"
    assert spec.limits.max_concurrency == 2
    assert isinstance(spec.root.steps[-1], FanInStep)
    assert spec.root.steps[-1].owner == "parent"


# 功能：TOML IR 与 reduce 别名均进入同一严格 WorkflowSpec
# 设计：分别解析最小 TOML sequence 和 JSON reduce，证明只支持数据配置而非脚本执行
def test_toml_and_reduce_alias_are_supported() -> None:
    toml_text = """
schema_version = 1
id = "workflow-toml"
name = "toml workflow"

[root]
type = "sequence"
id = "root"

[[root.steps]]
type = "worker"
id = "worker-a"
description = "worker a"
prompt = "do a"
"""
    toml_spec = parse_workflow_text(toml_text, format="toml")
    reduce_spec = parse_workflow_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "workflow-reduce",
                "name": "reduce alias",
                "root": {
                    "type": "reduce",
                    "id": "reduce-root",
                    "owner": "parent",
                    "steps": [
                        {
                            "type": "worker",
                            "id": "worker-a",
                            "description": "worker a",
                            "prompt": "do a",
                        }
                    ],
                },
            }
        ),
        format="json",
    )

    assert toml_spec.root.type == "sequence"
    assert reduce_spec.root.type == "fan_in"


# 功能：IR 拒绝任意 script 字段和非 JSON/TOML 格式
# 设计：利用 extra=forbid 与显式 format 白名单验证 V1 不存在代码执行逃逸口
def test_workflow_rejects_scripts_and_unknown_formats() -> None:
    payload = _complete_workflow()
    payload["script"] = "__import__('os').system('bad')"

    with pytest.raises(WorkflowParseError, match="invalid workflow"):
        parse_workflow_text(json.dumps(payload), format="json")
    with pytest.raises(WorkflowParseError, match="unsupported workflow format"):
        parse_workflow_text("{}", format="python")


# 功能：高风险写入节点在 review_gate 外部时被拒绝
# 设计：从完整配置抽出 risky worker 作为根，验证风险 gate 是结构约束而非 prompt 约定
def test_high_risk_write_requires_review_gate() -> None:
    complete = _complete_workflow()
    root = complete["root"]
    assert isinstance(root, dict)
    steps = root["steps"]
    assert isinstance(steps, list)
    review = steps[4]
    assert isinstance(review, dict)
    payload = {
        "schema_version": 1,
        "id": "workflow-risky",
        "name": "risky",
        "root": review["step"],
    }

    with pytest.raises(WorkflowParseError, match="requires reviewer gate"):
        parse_workflow_text(json.dumps(payload), format="json")


# 功能：节点数、深度和 parallel 并发均受 WorkflowLimits 硬约束
# 设计：针对三个独立边界修改同一有效配置，逐一断言解析阶段 fail closed
@pytest.mark.parametrize(
    ("limit_key", "limit_value", "message"),
    [
        ("max_nodes", 2, "node limit exceeded"),
        ("max_depth", 2, "depth limit exceeded"),
        ("max_concurrency", 1, "parallel concurrency exceeds"),
    ],
)
def test_workflow_limits_fail_closed(
    limit_key: str,
    limit_value: int,
    message: str,
) -> None:
    payload = _complete_workflow()
    limits = payload["limits"]
    assert isinstance(limits, dict)
    limits[limit_key] = limit_value

    with pytest.raises(WorkflowParseError, match=message):
        parse_workflow_text(json.dumps(payload), format="json")


# 功能：fan_in 缺少显式 owner 时无法通过模型校验
# 设计：删除 owner 字段并断言解析失败，防止无人负责聚合结果和后续合并
def test_fan_in_requires_owner() -> None:
    payload = _complete_workflow()
    root = payload["root"]
    assert isinstance(root, dict)
    steps = root["steps"]
    assert isinstance(steps, list)
    fan_in = steps[-1]
    assert isinstance(fan_in, dict)
    fan_in.pop("owner")

    with pytest.raises(WorkflowParseError, match="invalid workflow"):
        parse_workflow_text(json.dumps(payload), format="json")


# 功能：parallel 在执行前拒绝重叠写声明，但允许共享显式 coordination contract
# 设计：对同一文件先构造无协调冲突，再为两个 worker 设置相同契约验证受控并行入口
def test_parallel_write_claims_fail_closed_without_coordination() -> None:
    claim = {"read_only": False, "exact_files": ["release.toml"]}
    payload = {
        "schema_version": 1,
        "id": "workflow-claims",
        "name": "claims",
        "root": {
            "type": "parallel",
            "id": "parallel",
            "steps": [
                {
                    "type": "worker",
                    "id": "writer-a",
                    "description": "writer a",
                    "prompt": "write a",
                    "write_claim": claim,
                },
                {
                    "type": "worker",
                    "id": "writer-b",
                    "description": "writer b",
                    "prompt": "write b",
                    "write_claim": claim,
                },
            ],
        },
    }

    with pytest.raises(WorkflowParseError, match="parallel write claims overlap"):
        parse_workflow_text(json.dumps(payload), format="json")

    coordinated = json.loads(json.dumps(payload))
    for worker in coordinated["root"]["steps"]:
        worker["write_claim"]["coordination_contract"] = "release-owner"

    assert parse_workflow_text(json.dumps(coordinated), format="json").id == "workflow-claims"
