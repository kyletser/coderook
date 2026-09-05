from __future__ import annotations

import sys
from pathlib import Path


# 检查解释答案包含任务要求的全部关键事实且不是空占位
def main() -> int:
    task_id = sys.argv[1]
    root = Path(__file__).resolve().parent
    answer = (root / f"{task_id}.md").read_text(encoding="utf-8").lower()
    required_groups = {
        "explain-cache": (("ttl",), ("monotonic",), ("expired", "过期")),
        "explain-lock": (("atomic", "原子"), ("replace", "替换"), ("process", "进程")),
        "explain-cache-concurrency": (
            ("thread", "线程"),
            ("race", "竞态", "竞争"),
            ("lock", "锁"),
        ),
        "explain-cache-memory": (
            ("expired", "过期"),
            ("lazy", "惰性"),
            ("memory", "内存"),
        ),
        "explain-atomic-durability": (
            ("fsync",),
            ("directory", "目录"),
            ("durability", "持久"),
        ),
        "explain-atomic-failure": (
            ("temporary", "临时"),
            ("cleanup", "清理"),
            ("exception", "异常"),
        ),
    }[task_id]
    missing = [
        "/".join(group)
        for group in required_groups
        if not any(term in answer for term in group)
    ]
    if len(answer.strip()) < 120 or missing:
        print(f"answer too short or missing facts: {missing}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
