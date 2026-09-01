from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.run_compaction_experiment import _select_tasks
from scripts.run_reliability_suite import _commands


# 功能：验证默认可靠性套件只包含三个有结果意义的 8 美元 Pilot 阶段
# 设计：直接检查命令计划而不启动子进程，锁定付费边界并排除标签代理诊断
def test_reliability_suite_defaults_to_bounded_pilot(tmp_path: Path) -> None:
    args = argparse.Namespace(
        output=tmp_path,
        profile="pilot",
        polyglot_dataset=None,
        polyglot_commit=None,
    )

    commands = _commands(args)

    assert sum(stage[3] for stage in commands if stage[2]) == 8.0
    assert [stage[0] for stage in commands] == [
        "compaction",
        "multiagent",
        "internal_quick",
    ]


# 功能：验证压缩 Pilot 使用数据集显式声明的固定任务而非目录偶然顺序
# 设计：读取真实冻结数据并比较前六项 ID，确保后续扩容前后样本身份稳定
def test_compaction_pilot_uses_declared_task_ids() -> None:
    root = Path(__file__).resolve().parents[2]
    dataset = json.loads(
        (root / "benchmarks/reliability/long_context_tasks.json").read_text(
            encoding="utf-8"
        )
    )

    selected = _select_tasks(dataset, 6)

    assert [task["id"] for task in selected] == dataset["pilot_task_ids"]
