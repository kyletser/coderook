from __future__ import annotations

import json
from pathlib import Path

from scripts.prepare_swebench_slice import select_instances
from scripts.run_swebench_benchmark import official_result, summarize
from scripts.swebench_container_agent import BENCHMARK_TOOLS

from code_rook.core.config import CodeRookConfig
from code_rook.core.runner import AgentRunner
from code_rook.core.task.manager import TaskManager


# 功能：验证固定切片跨仓库选择且不受记录顺序、标准答案或结果标签影响
# 设计：反转输入并注入虚构答案后比较实例 ID，避免选择逻辑暗中依赖解题结果
def test_slice_selection_is_outcome_blind_and_order_independent() -> None:
    rows = [{'instance_id': f'{repo}-{i}', 'repo': repo} for repo in ['a', 'b', 'c']
            for i in range(5)]
    selected = select_instances(rows, 5, set())
    changed = [dict(row, patch='not used', resolved=True) for row in reversed(rows)]
    assert [row['instance_id'] for row in selected] == [
        row['instance_id'] for row in select_instances(changed, 5, set())]
    assert len({row['repo'] for row in selected[:3]}) == 3
    excluded = {row['instance_id'] for row in selected}
    assert not excluded.intersection(
        row['instance_id'] for row in select_instances(rows, 5, excluded))


# 功能：验证只使用指定 run 的官方 resolved 字段，缺失报告不能误报为通过
# 设计：用两个同实例不同 run 的报告模拟官方缓存，确保不会取到另一轮的成功结果
def test_official_result_is_scoped_and_missing_is_unknown(tmp_path: Path) -> None:
    assert official_result(tmp_path, 'current', 'demo-1') is None
    for run_id, resolved in [('old', True), ('current', False)]:
        path = tmp_path / 'logs/evaluation' / run_id / 'model/demo-1/report.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'demo-1': {'resolved': resolved}}), encoding='utf-8')
    assert official_result(tmp_path, 'current', 'demo-1') is False
    assert official_result(tmp_path, 'old', 'demo-1') is True


# 功能：验证运行成功、官方解题通过和工具错误分别汇总，缺失判分不能补成成功
# 设计：构造自报成功但官方失败、步数耗尽但补丁通过两种真实 Pilot 边界，再加入未判分实例
def test_summary_separates_runtime_and_official_outcomes(tmp_path: Path) -> None:
    rows = [{'instance_id': name} for name in ['runtime-only', 'patch-only', 'ungraded']]
    for name, status, resolved in [('runtime-only', 'success', False), ('patch-only', 'failed', True)]:
        folder = tmp_path / 'pilot' / name
        (folder / 'evidence').mkdir(parents=True)
        (folder / 'record.json').write_text(json.dumps({'execution': {
            'status': status, 'usage': [{'input_tokens': 10, 'output_tokens': 2}],
        }}), encoding='utf-8')
        (folder / 'evidence/events.jsonl').write_text(json.dumps({
            'type': 'tool.call_failed', 'error_class': 'schema_error',
            'error_message': 'action is required for tool: Bash',
        }), encoding='utf-8')
        report = tmp_path / 'logs/evaluation/current/model' / name / 'report.json'
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps({name: {'resolved': resolved}}), encoding='utf-8')
    result = summarize(tmp_path, 'pilot', 'current', rows)
    assert (result['resolved'], result['unresolved'], result['ungraded']) == (1, 1, 1)
    assert result['runtime_successes'] == 1
    assert result['input_tokens'] == 20
    assert result['schema_errors'] == 2
    assert result['results'][1]['missing_action_errors'] == 1


# 功能：验证基准入口的工具白名单实际暴露 Repository 而不是静默遗漏大小写不匹配的名称
# 设计：调用真实 Runner 装配工具目录，逐项比较冻结白名单而不启动模型或仓库扫描
def test_swebench_tools_match_actual_model_catalog(tmp_path: Path) -> None:
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(TaskManager(tmp_path / '.tasks'),
                                     tool_whitelist=list(BENCHMARK_TOOLS))
    assert {schema['name'] for schema in registry.tool_schemas()} == set(BENCHMARK_TOOLS)
